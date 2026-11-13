import asyncio

import pytest

from research_copilot.runtime.maintenance import MaintenanceLost
from research_copilot.runtime.runtime_watch import RuntimeWatch


class Guard:
    def __init__(self):
        self.started, self.finish = asyncio.Event(), asyncio.Event()
        self.holds = set()
        self.delay = "acquire"
        self.fail_release = False

    async def execute(self, query, parameters):
        acquire = "pg_try_advisory_lock" in query
        if (acquire and self.delay == "acquire") or (
            not acquire and self.delay == "release"
        ):
            self.started.set()
            await self.finish.wait()
        key = parameters[0]
        if acquire:
            self.holds.add(key)
            value = True
        elif self.fail_release:
            raise ConnectionError("Synthetic release loss")
        else:
            value = key in self.holds
            self.holds.discard(key)

        class Result:
            async def fetchone(self):
                return (value,)

        return Result()


@pytest.mark.parametrize("phase", ["acquire", "release"])
async def test_cancellation_waits_for_lock_result_and_unlock(monkeypatch, phase):
    from research_copilot.runtime import owned_report_locks as module

    async def connected(connection):
        return

    monkeypatch.setattr(module, "assert_connected", connected)
    guard = Guard()
    guard.delay = phase
    watch = RuntimeWatch(lambda: None)
    locks = module.OwnedReportLocks(guard, watch)

    async def work():
        async with locks.hold("report-1"):
            pass

    task = asyncio.create_task(work())
    await guard.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "Cancellation must not abandon an in-flight lock query"
    closing = asyncio.create_task(locks.close())
    await asyncio.sleep(0)
    assert not closing.done()
    guard.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await closing
    assert not guard.holds and not watch.lost.is_set()
    await watch.close()


async def test_release_failure_invalidates_host_and_preserves_body_error(monkeypatch):
    from research_copilot.runtime import owned_report_locks as module

    async def connected(connection):
        return

    monkeypatch.setattr(module, "assert_connected", connected)
    guard = Guard()
    guard.delay = None
    guard.fail_release = True
    watch = RuntimeWatch(lambda: None)
    locks = module.OwnedReportLocks(guard, watch)
    with pytest.raises(ValueError, match="original body"):
        async with locks.hold("report-1"):
            raise ValueError("original body")
    assert watch.lost.is_set()
    with pytest.raises(MaintenanceLost):
        async with locks.hold("report-2"):
            pytest.fail("Lost owner cannot enter another report")
    await locks.close()
    await watch.close()
