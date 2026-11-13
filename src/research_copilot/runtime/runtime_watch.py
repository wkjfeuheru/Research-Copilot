"""Sticky detection of runtime ownership loss and coordinated local draining."""

import asyncio
import logging

from research_copilot.runtime.maintenance import (
    MaintenanceBusy,
    MaintenanceLost,
    DATABASE_ACCESS_KEY,
)
from research_copilot.runtime.runtime_ownership import CONNECTED, assert_connected

logger = logging.getLogger(__name__)


def require_live(store):
    watch = getattr(store, "runtime_watch", None)
    if watch is not None:
        watch.check()


def watch_owner(connection, identity):
    async def probe():
        row = await (
            await connection.execute(
                "SELECT "
                + CONNECTED
                + " FROM public.runtime_owners o WHERE id=%s AND state='open'",
                (DATABASE_ACCESS_KEY, identity),
            )
        ).fetchone()
        if not row or row[0] is not True:
            raise MaintenanceLost("Runtime ownership no longer valid")

    async def admission_probe():
        await probe()
        await assert_connected(connection)

    return RuntimeWatch(probe, admission_probe=admission_probe)


class RuntimeWatch:
    def __init__(self, probe, *, admission_probe=None, interval=1.0, timeout=2.0):
        if interval <= 0 or timeout <= 0:
            raise ValueError("Positive ownership probe bounds required")
        self.probe, self.interval, self.timeout = probe, interval, timeout
        self.admission_probe = admission_probe or probe
        self.lost = asyncio.Event()
        self._closing = False
        self._callbacks = set()
        self._poller = None
        self._settlement = None
        self._close_lock = asyncio.Lock()

    @property
    def available(self):
        return not self.lost.is_set() and not self._closing

    def check(self):
        if not self.available:
            raise MaintenanceLost(
                "Runtime ownership lost or closing; admission stopped"
            )

    def subscribe(self, stop):
        self.check()
        self._callbacks.add(stop)

    async def check_admission(self):
        """Recheck after obtaining a physical slot, before starting external work."""
        self.check()
        try:
            async with asyncio.timeout(self.timeout):
                await self.admission_probe()
        except MaintenanceBusy:
            # Another owner is unsettled. Keep this healthy owner available for
            # draining; only explicit recovery may clear the other owner's row.
            raise
        except Exception:
            self.invalidate()
            raise MaintenanceLost(
                "Runtime ownership admission failed; host recovery required"
            ) from None
        self.check()

    def start(self):
        self.check()
        if self._poller is None:
            self._poller = asyncio.create_task(
                self._poll(), name="runtime-ownership-probe"
            )

    def _settle(self):
        if self._settlement is None:
            self._settlement = asyncio.create_task(
                self._stop_all(), name="runtime-ownership-drain"
            )
        return self._settlement

    def invalidate(self):
        """Report an observed lock failure without waiting on this task's own drain."""
        self.lost.set()
        self._settle()

    async def _stop_all(self):
        results = await asyncio.gather(
            *(stop() for stop in tuple(self._callbacks)), return_exceptions=True
        )
        self._callbacks.clear()
        if any(isinstance(result, BaseException) for result in results):
            self.lost.set()
            raise MaintenanceLost(
                "Runtime ownership shutdown failed; dependencies retained for inspection"
            )

    async def _poll(self):
        try:
            while True:
                await asyncio.sleep(self.interval)
                async with asyncio.timeout(self.timeout):
                    await self.probe()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.invalidate()
            logger.error(
                "Runtime ownership probe failed; stopping admission and draining local work"
            )

    async def close(self):
        async with self._close_lock:
            self._closing = True
            if self._poller is not None:
                self._poller.cancel()
                await asyncio.gather(self._poller, return_exceptions=True)
            # Do not cancel a delayed settlement when closing the monitor.
            await asyncio.shield(self._settle())
