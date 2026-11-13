"""One report's cooperative cancellation and bounded task dispatch."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from research_copilot.contracts import Dispatch

active_dispatch: ContextVar[Dispatch | None] = ContextVar("dispatch", default=None)


class RunCancelled(RuntimeError):
    pass


class RunInterrupted(RuntimeError):
    """Host shutdown interrupted a run; unlike cancellation it remains recoverable."""


class RunControl:
    def __init__(self, max_parallel_tasks: int = 3, *, cancellation_probe=None):
        if not 1 <= max_parallel_tasks <= 3:
            raise ValueError("max_parallel_tasks must be between 1 and 3")
        self._slots = asyncio.Semaphore(max_parallel_tasks)
        self.cancelled = asyncio.Event()
        self.stopping = asyncio.Event()
        self.cancellation_probe = cancellation_probe

    def check(self):
        if self.cancelled.is_set():
            raise RunCancelled("Report run cancelled")
        if self.stopping.is_set():
            raise RunInterrupted("Host is shutting down; run can be recovered")

    def cancel(self):
        self.cancelled.set()

    def stop(self):
        self.stopping.set()

    async def acheck(self):
        if self.cancellation_probe and await self.cancellation_probe():
            self.cancel()
        self.check()

    @asynccontextmanager
    async def dispatch(self, dispatch: Dispatch):
        await self.acheck()
        async with self._slots:
            await self.acheck()
            token = active_dispatch.set(dispatch)
            try:
                yield
            finally:
                active_dispatch.reset(token)
