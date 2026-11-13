"""Real receipt and persistent capacity path with explicit offline Docker fixtures."""

import asyncio
import threading

from filelock import FileLock, Timeout
import pytest
from sqlalchemy import select

from research_copilot.runtime.control import active_dispatch, RunControl, RunCancelled
from research_copilot.runtime.sandbox_slots import claims
from research_copilot.sandbox.config import SandboxConfig
from research_copilot.sandbox.receipts import ExecutionService
from research_copilot.sandbox.runner import RunOutput, ExecutionUnknown
from tests.integration.test_analysist import financial_evidence
from tests.integration.test_searcher import dispatch
from tests.unit.test_orchestration import context
from tests.unit.test_sandbox_slots import slot_managers

__all__ = ["slot_managers"]


class Runner:
    def __init__(self, outcome):
        self.outcome, self.calls, self.confirmations = outcome, 0, 0

    async def cleanup_orphans(self):
        return []

    async def image_identity(self):
        return "sha256:" + "a" * 64

    async def run(self, **kwargs):
        self.calls += 1
        if self.outcome == "unknown":
            raise ConnectionError("Ambiguous Docker command")
        if self.outcome == "cancel":
            raise RunCancelled()
        return RunOutput(
            1,
            "offline nonzero command",
            False,
            b"",
            "failed",
            1,
            await self.image_identity(),
        )

    async def confirm_cleanup(self, execution_id):
        self.confirmations += 1
        if self.outcome == "cleanup_unknown":
            raise ExecutionUnknown("Cannot inspect cleanup")


@pytest.mark.parametrize(
    "outcome", ["returned", "unknown", "cancel", "cleanup_unknown"]
)
async def test_receipt_keeps_unknown_capacity_and_releases_only_confirmed_cleanup(
    role_env, slot_managers, outcome
):
    await financial_evidence(role_env)
    scope = await dispatch(role_env, "analysist", "durable-slot")
    token = active_dispatch.set(scope)
    manager, _ = slot_managers
    runner = Runner(outcome)
    executor = ExecutionService(
        role_env[0],
        control=RunControl(),
        budget=None,
        config=SandboxConfig(concurrency=1),
        runner=runner,
        slot_claims=manager,
    )
    try:
        if outcome == "returned":
            receipt = await executor.execute(context(), "call-1", "false")
            assert receipt["status"] == "failed"
        else:
            with pytest.raises(
                {
                    "unknown": ConnectionError,
                    "cancel": RunCancelled,
                    "cleanup_unknown": ExecutionUnknown,
                }[outcome]
            ):
                await executor.execute(context(), "call-1", "false")
        state = await role_env[2].read_control(context())
        execution = next(iter(state.role_data["executions"].values()))
        assert execution["status"] == ("failed" if outcome == "returned" else "unknown")
        async with manager.engine.connect() as connection:
            row = (await connection.execute(select(claims))).mappings().one()
        assert row["state"] == ("released" if outcome == "returned" else "held")
        assert runner.calls == 1
        assert runner.confirmations == (
            1 if outcome in {"returned", "cleanup_unknown"} else 0
        )
    finally:
        active_dispatch.reset(token)


async def test_cancel_waits_for_snapshot_thread_before_releasing_capacity(
    role_env, slot_managers
):
    await financial_evidence(role_env)
    scope = await dispatch(role_env, "analysist", "snapshot-cancel")
    token = active_dispatch.set(scope)
    manager, _ = slot_managers
    runner = Runner("returned")
    executor = ExecutionService(
        role_env[0],
        control=RunControl(),
        budget=None,
        config=SandboxConfig(concurrency=1),
        runner=runner,
        slot_claims=manager,
    )
    entered, finish = threading.Event(), threading.Event()
    original = executor._freeze

    def freeze(*args):
        entered.set()
        assert finish.wait(10)
        return original(*args)

    executor._freeze = freeze
    task = asyncio.create_task(executor.execute(context(), "call-1", "false"))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        assert not await manager.available(0)
        with pytest.raises(Timeout):
            with FileLock(
                role_env[0].workspace_root / ".sandbox-locks/slot-0.lock", timeout=0
            ):
                pytest.fail("Snapshot still running after slot release")
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        active_dispatch.reset(token)
    assert task.cancelled() and runner.calls == 0
    assert await manager.available(0)
