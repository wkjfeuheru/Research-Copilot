import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.contracts import RunContext


@pytest.mark.asyncio
async def test_cancelled_control_read_closes_owned_connection_before_propagation():
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    class Connection:
        async def execute(self, statement):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                events.append("driver_cancelled")
                raise
            return SimpleNamespace(scalar_one=lambda: {})

    class Engine:
        @asynccontextmanager
        async def connect(self):
            try:
                yield Connection()
            finally:
                events.append("closed")

    store = SqlSessionStore(Engine())
    ctx = RunContext("actor", "report", "run", "budget", "policy")
    task = asyncio.create_task(store.read_control(ctx))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["closed"]
