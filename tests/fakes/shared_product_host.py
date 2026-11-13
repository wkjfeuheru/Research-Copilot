"""Real Web lifespan with report-scoped, persistent synthetic provider receipts.

Only model/source boundaries and fixture scheduling differ: each worker has two
local slots; post-delivery extraction is disabled. No business rows are seeded.
Docker runs the actual generated script, waiting for a test-controlled /tmp file.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from filelock import FileLock
from langchain_core.messages import AIMessage
import uvicorn

from research_copilot import host
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.loop import run_async
from research_copilot.runtime.run_worker import RunWorker
from research_copilot.subagents.searcher import SourceTool
from research_copilot.web.app import build_server_app
from research_copilot.web import runs
from tests.integration.test_full_product_flow import FrozenScenario
from tests.unit.test_orchestration import ScriptModel


class Provider:
    def __init__(self, directory, report_id):
        self.path = directory / (report_id + ".json")

    def respond(self, role, messages):
        with FileLock(str(self.path) + ".lock", timeout=5):
            state = (
                json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
            )
            scenario = FrozenScenario()
            for key in ("facts", "execution", "analysis", "image_reads"):
                if key in state:
                    setattr(scenario, key, state[key])
            positions = state.setdefault("positions", {})
            step = positions.get(role, 0)
            response = getattr(scenario, "planner" if role == "supervisor" else role)(
                messages, step
            )
            if role == "analysist" and step == 0:
                response.tool_calls[0]["args"]["content"] = (
                    "import time\nfrom pathlib import Path\n"
                    "deadline=time.monotonic()+90\n"
                    "while not Path('/tmp/fixture-release').exists():\n"
                    "    if time.monotonic()>deadline: raise TimeoutError('fixture release missing')\n"
                    "    time.sleep(0.05)\n" + response.tool_calls[0]["args"]["content"]
                )
            positions[role] = step + 1
            state.update(
                {
                    key: getattr(scenario, key)
                    for key in ("facts", "execution", "analysis", "image_reads")
                }
            )
            state.setdefault("calls", []).append(
                {"role": role, "step": step, "pid": os.getpid()}
            )
            pending = self.path.with_suffix(".pending")
            pending.write_text(json.dumps(state), encoding="utf-8")
            os.replace(pending, self.path)
            return response

    def models(self):
        scenario = FrozenScenario()
        return {
            role: scenario.model(
                lambda messages, _step, role=role: self.respond(role, messages),
                images=role == "reviewer",
            )
            for role in ("supervisor", "searcher", "analysist", "writer", "reviewer")
        }


async def main():
    controls, workspace, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    providers = controls / "providers"
    providers.mkdir(exist_ok=True)

    class ScopedFactory(SupervisorFactory):
        async def __call__(self, record, control, *, read_only=False):
            provider = Provider(providers, record.report_id)
            scenario = FrozenScenario()
            child = replace(
                self,
                models=provider.models(),
                source_tools=[
                    SourceTool(
                        "sina_filing",
                        "sina_finance",
                        "filing",
                        "Frozen filing fixture",
                        {
                            "type": "object",
                            "properties": {
                                "period": {"type": "string", "enum": ["2023", "2024"]}
                            },
                            "required": ["period"],
                            "additionalProperties": False,
                        },
                        scenario.fetch,
                    )
                ],
            )
            # Preserve the actual host's report cancellation registry.
            child._sandbox_services = self._sandbox_services
            child._status_readers = self._status_readers
            return await SupervisorFactory.__call__(
                child, record, control, read_only=read_only
            )

    @asynccontextmanager
    async def models(config):
        yield {
            role: ScriptModel(script=[AIMessage(content="unused")])
            for role in ("supervisor", "searcher", "analysist", "writer", "reviewer")
        }

    original_session = host._open_session

    @asynccontextmanager
    async def no_retrospective(*args, **kwargs):
        async with original_session(*args, **kwargs) as value:
            value[2].experience_extractor = None
            yield value

    class TwoLocalSlots(RunWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, max_local=2)

    host.open_models = models
    host.SupervisorFactory = ScopedFactory
    host._open_session = no_retrospective
    runs.RunWorker = TwoLocalSlots
    settings = HostSettings(
        _env_file=None,
        database_url=os.environ["RC_TEST_DATABASE_URL"],
    )
    app = build_server_app(
        settings=settings,
        models_path=Path("config/models.example.toml"),
        workspace_root=workspace,
        actor_id="actor-1",
        policy_version="policy-1",
        token_limit=1_000_000,
        mode="test",
        rules_path=Path("config/compliance_rules.yaml"),
        rules_approval_path=None,
        execution_policy_path=Path("config/execution_policy.toml"),
        static_dir=None,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            loop="asyncio",
            log_level="warning",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("Server stopped before startup")
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    (controls / (label + ".ready")).write_text(
        json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8"
    )
    while not task.done() and not (controls / (label + ".stop")).exists():
        await asyncio.sleep(0.02)
    server.should_exit = True
    await task
    (controls / (label + ".closed")).write_text(str(os.getpid()), encoding="utf-8")


if __name__ == "__main__":
    run_async(main())
