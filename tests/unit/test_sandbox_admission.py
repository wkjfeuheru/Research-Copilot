import asyncio
from types import SimpleNamespace

from filelock import FileLock, Timeout
import pytest

from research_copilot.runtime.control import RunControl
from research_copilot.runtime.maintenance import MaintenanceBusy
from research_copilot.sandbox.config import SandboxConfig
from research_copilot.sandbox.receipts import ExecutionService


@pytest.mark.parametrize("cancel", [False, True])
async def test_admission_runs_with_slot_held_and_failure_releases_locks(
    tmp_path, cancel
):
    root = tmp_path / ".sandbox-locks"
    entered, finish = asyncio.Event(), asyncio.Event()

    async def admission():
        for name in ("slot-0.lock", "report-1-execution.lock"):
            with pytest.raises(Timeout):
                with FileLock(root / name, timeout=0):
                    pytest.fail("Admission ran before acquiring physical slot")
        entered.set()
        await finish.wait()
        raise MaintenanceBusy("Unsettled runtime owners")

    service = ExecutionService(
        SimpleNamespace(workspace_root=tmp_path),
        control=RunControl(),
        budget=None,
        config=SandboxConfig(concurrency=1),
        admission_check=admission,
    )

    async def execute():
        async with service._locks("report-1", "dispatch-1"):
            pytest.fail("Rejected admission reached external execution")

    task = asyncio.create_task(execute())
    await asyncio.wait_for(entered.wait(), 2)
    if cancel:
        task.cancel()
    else:
        finish.set()
    with pytest.raises(asyncio.CancelledError if cancel else MaintenanceBusy):
        await task
    for name in ("slot-0.lock", "report-1-execution.lock"):
        with FileLock(root / name, timeout=0):
            pass
