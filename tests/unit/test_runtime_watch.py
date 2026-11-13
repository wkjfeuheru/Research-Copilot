import asyncio

import pytest


async def test_loss_is_sticky_and_close_waits_for_all_stop_callbacks():
    from research_copilot.runtime.runtime_watch import RuntimeWatch

    fail, entered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def probe():
        await fail.wait()
        raise ConnectionError("Synthetic lock connection failure")

    async def stop():
        calls.append("stop")
        entered.set()
        await finish.wait()

    watch = RuntimeWatch(probe, interval=0.01, timeout=1)
    watch.subscribe(stop)
    watch.start()
    fail.set()
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(RuntimeError, match="ownership"):
        watch.check()
    close = asyncio.create_task(watch.close())
    await asyncio.sleep(0.03)
    assert not close.done()
    finish.set()
    await asyncio.wait_for(close, 1)
    await watch.close()
    assert calls == ["stop"]
    assert watch.lost.is_set()


async def test_probe_timeout_stops_host_without_retry():
    from research_copilot.runtime.runtime_watch import RuntimeWatch

    calls = []

    async def probe():
        calls.append("probe")
        await asyncio.Event().wait()

    watch = RuntimeWatch(probe, interval=0.01, timeout=0.03)
    watch.start()
    await asyncio.wait_for(watch.lost.wait(), 1)
    await watch.close()
    assert calls == ["probe"]


async def test_healthy_context_close_stops_subscribers_before_returning():
    from research_copilot.runtime.runtime_watch import RuntimeWatch

    calls = []

    async def probe():
        return

    async def stop():
        calls.append("stopped")

    watch = RuntimeWatch(probe)
    watch.subscribe(stop)
    watch.start()
    await watch.close()
    assert calls == ["stopped"] and not watch.lost.is_set()


async def test_other_unsettled_owner_rejects_admission_without_stopping_healthy_host():
    from research_copilot.runtime.maintenance import MaintenanceBusy
    from research_copilot.runtime.runtime_watch import RuntimeWatch

    async def probe():
        pass

    async def admission_probe():
        raise MaintenanceBusy("Unsettled runtime owners")

    watch = RuntimeWatch(probe, admission_probe=admission_probe)
    with pytest.raises(MaintenanceBusy):
        await watch.check_admission()
    assert watch.available
    await watch.close()


async def test_admission_timeout_invalidates_owner_without_retry():
    from research_copilot.runtime.maintenance import MaintenanceLost
    from research_copilot.runtime.runtime_watch import RuntimeWatch

    calls = []

    async def probe():
        calls.append("probe")
        await asyncio.Event().wait()

    watch = RuntimeWatch(probe, timeout=0.03)
    with pytest.raises(MaintenanceLost):
        await watch.check_admission()
    with pytest.raises(MaintenanceLost):
        await watch.check_admission()
    assert calls == ["probe"]
    await watch.close()
