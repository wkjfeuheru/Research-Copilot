"""Isolated process holding a real sandbox slot; no containers or models started."""

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from research_copilot.runtime.control import RunControl
from research_copilot.runtime.loop import run_async
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.sandbox.config import SandboxConfig
from research_copilot.sandbox.receipts import ExecutionService
from research_copilot.sandbox.runner import DockerRunner


async def main():
    root = Path(sys.argv[1])
    execution_id = sys.argv[2]
    async with postgres_runtime(os.environ["RC_TEST_DATABASE_URL"]) as (store, _):
        service = ExecutionService(
            SimpleNamespace(workspace_root=root),
            control=RunControl(),
            budget=None,
            config=SandboxConfig(concurrency=1),
            admission_check=store.check_external_admission,
            slot_claims=store.sandbox_slots,
        )
        async with store.lock("report-1"):
            async with service._locks("report-1", "dispatch-1") as slot:
                async with store.sandbox_slots.hold(
                    slot, "report-1", "dispatch-1", execution_id
                ) as lease:
                    lease.external_started = True
                    if "docker" in sys.argv[3:]:
                        snapshot = root / "snapshot"
                        (snapshot / "workspace/tasks/dispatch-1").mkdir(parents=True)
                        (snapshot / "skills").mkdir()
                        runner = DockerRunner(SandboxConfig(timeout_seconds=600))
                        task = asyncio.create_task(
                            runner.run(
                                execution_id=execution_id,
                                report_id="report-1",
                                dispatch_id="dispatch-1",
                                snapshot=snapshot,
                                command="sleep 600",
                                control=RunControl(),
                                timeout=600,
                            )
                        )
                        name = "rc-analysis-" + execution_id.removeprefix("exec-")
                        while True:
                            if task.done():
                                await task
                                raise RuntimeError("Docker fixture exited before ready")
                            code, out, _, _ = await runner._command(
                                "inspect", name, "--format", "{{.State.Running}}"
                            )
                            if code == 0 and out.strip() == b"true":
                                break
                            await asyncio.sleep(0.05)
                    (root / "ready").write_text("locked", encoding="utf-8")
                    await asyncio.Event().wait()


if __name__ == "__main__":
    run_async(main())
