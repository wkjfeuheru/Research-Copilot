"""Report locks share the non-reconnecting, durably registered host connection."""

import asyncio
from contextlib import asynccontextmanager
import logging

from research_copilot.runtime.identity import advisory_lock_identity
from research_copilot.runtime.maintenance import MaintenanceLost
from research_copilot.runtime.runtime_ownership import assert_connected
from research_copilot.runtime.storage import SessionBusy
from research_copilot.runtime.task_completion import await_completion

logger = logging.getLogger(__name__)


class OwnedReportLocks:
    def __init__(self, connection, watch):
        self.connection, self.watch = connection, watch
        self._held = set()
        self._empty = asyncio.Event()
        self._empty.set()
        self._closing = False

    async def close(self):
        self._closing = True
        await self._empty.wait()

    async def _lock_query(self, statement, key):
        try:
            row = await (await self.connection.execute(statement, (key,))).fetchone()
            return bool(row and row[0])
        except Exception:
            self.watch.invalidate()
            raise MaintenanceLost(
                "Report lock connection lost; host recovery required"
            ) from None

    @asynccontextmanager
    async def hold(self, report_id):
        self.watch.check()
        if self._closing:
            raise MaintenanceLost("Report lock owner is closing")
        key = advisory_lock_identity(report_id)
        # Session advisory locks are reentrant on one connection. Reserve before
        # the first await, including while an acquire/release query is in flight.
        if key in self._held:
            raise SessionBusy("Report is already running")
        self._held.add(key)
        self._empty.clear()
        acquired = False
        failed = False

        async def acquire():
            nonlocal acquired
            acquired = await self._lock_query("SELECT pg_try_advisory_lock(%s)", key)
            if not acquired:
                raise SessionBusy("Report is already running")
            # Check AFTER obtaining the report lock: a previous holder may have
            # disconnected since this host started or while acquisition was queued.
            # Its durable row must continue fencing old work until explicit recovery.
            await assert_connected(self.connection)
            self.watch.check()

        async def release():
            if not await self._lock_query("SELECT pg_advisory_unlock(%s)", key):
                self.watch.invalidate()
                raise MaintenanceLost(
                    "Report lock ownership lost; host recovery required"
                )

        try:
            await await_completion(acquire())
            yield
        except BaseException:
            failed = True
            raise
        finally:
            try:
                if acquired:
                    try:
                        await await_completion(release())
                    except BaseException:
                        if not failed:
                            raise
                        logger.error(
                            "Report lock cleanup failed; original operation error retained"
                        )
            finally:
                self._held.discard(key)
                if not self._held:
                    self._empty.set()
