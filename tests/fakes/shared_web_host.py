"""Actual Web/host/worker lifespan; only model transport is an offline fixture."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import uvicorn

from research_copilot import host
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.loop import run_async
from research_copilot.web.app import build_server_app
from tests.integration.test_full_product_flow import FrozenScenario
from tests.unit.test_orchestration import ScriptModel, call


async def main():
    directory, workspace, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    scenario = FrozenScenario()

    class Planner(ScriptModel):
        async def _agenerate(self, messages, **kwargs):
            requests = [
                str(message.content)
                for message in messages
                if isinstance(message, HumanMessage)
            ]
            match = re.search(r"multi-host-case-[1-5]", "\n".join(requests))
            if not match:
                raise ValueError("Unexpected fixture request")
            request = match.group()
            (directory / "calls" / (uuid4().hex + ".json")).write_text(
                json.dumps({"pid": os.getpid(), "request": request}),
                encoding="utf-8",
            )
            while not (directory / (request + ".release")).exists():
                await asyncio.sleep(0.02)
            response = call(
                "propose_plan", {"plan": scenario.plan.model_dump()}, "plan"
            )
            response.usage_metadata = {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            }
            return ChatResult(generations=[ChatGeneration(message=response)])

    class Unused(ScriptModel):
        async def _agenerate(self, messages, **kwargs):
            raise AssertionError("Only planner calls are permitted in this fixture")

    @asynccontextmanager
    async def models(config):
        yield {
            role: (Planner(script=[]) if role == "supervisor" else Unused(script=[]))
            for role in (
                "supervisor",
                "searcher",
                "analysist",
                "writer",
                "reviewer",
                "summarizer",
            )
        }

    host.open_models = models
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
    (directory / (label + ".ready")).write_text(
        json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8"
    )
    while not task.done() and not (directory / (label + ".stop")).exists():
        if (directory / "measure").exists():
            from tests.fakes.load_metrics import sample

            if time.monotonic() >= getattr(server, "next_measurement", 0):
                with (directory / (label + ".metrics.jsonl")).open(
                    "a", encoding="utf-8"
                ) as log:
                    log.write(json.dumps(sample(app.state.backend)) + "\n")
                server.next_measurement = time.monotonic() + 0.25
        await asyncio.sleep(0.02)
    server.should_exit = True
    while not task.done():
        if app.state.backend.runs.worker._closing:
            (directory / (label + ".draining")).touch()
        await asyncio.sleep(0.01)
    await task
    (directory / (label + ".closed")).write_text(str(os.getpid()), encoding="utf-8")


if __name__ == "__main__":
    run_async(main())
