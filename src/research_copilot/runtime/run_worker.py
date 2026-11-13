"""Execute persisted research commands with renewable ownership and orderly stop.

Loss of a lease cancels local work but never proves a provider did not charge.
SessionService and the existing request ledger own settlement and authorization.
"""

import asyncio
import logging
from uuid import uuid4

from research_copilot.runtime.run_queue import StaleRunClaim, _lease
from research_copilot.runtime.storage import SessionBusy
from research_copilot.runtime.runtime_watch import require_live

logger = logging.getLogger(__name__)


class RunWorker:
    def __init__(
        self,
        queue,
        session,
        *,
        lease_seconds=30,
        poll_interval=0.2,
        max_local=3,
        worker_id=None,
        job_id=None,
    ):
        _lease(lease_seconds)
        if not 0 < poll_interval <= 60 or not 1 <= max_local <= 100:
            raise ValueError("Invalid worker polling/capacity")
        self.queue, self.session = queue, session
        self.lease_seconds, self.poll_interval = lease_seconds, poll_interval
        self.max_local = max_local
        self.worker_id = worker_id or uuid4().hex
        self.job_id = job_id
        self._closing = False
        self._poller = None
        self._tasks = {}
        self._wake = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._close_started = False
        self._store = getattr(session, "store", None)
        watch = getattr(self._store, "runtime_watch", None)
        if watch is not None:
            watch.subscribe(self._stop_for_loss)

    async def _stop_for_loss(self):
        try:
            await self.aclose(grace_seconds=0)
        except TimeoutError:
            await self.drain()

    async def start(self):
        require_live(self._store)
        if self._closing:
            raise RuntimeError("Run worker is closed")
        if self._poller is None:
            self._poller = asyncio.create_task(self._poll())

    async def _claim(self):
        require_live(self._store)
        # A stalled database must not block shutdown indefinitely. A lost reply
        # may leave an unstarted claim; its lease safely returns it to pending.
        async with asyncio.timeout(self.lease_seconds / 3):
            return await self.queue.claim(
                self.worker_id, lease_seconds=self.lease_seconds, job_id=self.job_id
            )

    async def run_once(self):
        """One deterministic iteration, also usable by process supervisors/tests."""
        if self._closing:
            return False
        claim = await self._claim()
        if claim is None:
            return False
        if self._closing:
            return False
        task = asyncio.create_task(self._execute(claim))
        self._tasks[claim["claim_token"]] = task
        try:
            await task
        finally:
            self._tasks.pop(claim["claim_token"], None)
            self._wake.set()
        return True

    async def _poll(self):
        while not self._closing:
            self._wake.clear()
            try:
                while not self._closing and len(self._tasks) < self.max_local:
                    claim = await self._claim()
                    if claim is None:
                        break
                    if self._closing:
                        # No action has started. Leave the claim to expire rather
                        # than execute it during service shutdown.
                        break
                    task = asyncio.create_task(self._execute(claim))
                    self._tasks[claim["claim_token"]] = task
                    task.add_done_callback(
                        lambda done, key=claim["claim_token"]: self._finished(key, done)
                    )
            except Exception:
                logger.exception("Research queue polling failed")
            if not self._closing:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.poll_interval)
                except TimeoutError:
                    pass

    def _finished(self, key, task):
        self._tasks.pop(key, None)
        self._wake.set()
        if not task.cancelled() and task.exception() is not None:
            logger.error("Research command task failed", exc_info=task.exception())

    async def _heartbeat(self, claim):
        interval = self.lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            async with asyncio.timeout(interval):
                await self.queue.renew(claim, lease_seconds=self.lease_seconds)

    async def _execute(self, claim):
        started = False

        async def before_execute():
            nonlocal started
            require_live(self._store)
            if started or self._closing:
                raise RuntimeError("Command cannot begin execution")
            await self.queue.start(claim)
            started = True

        work = asyncio.create_task(
            self.session.execute_command(claim, before_execute=before_execute)
        )
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        error = None
        lost_lease = False
        deferred = False
        try:
            done, _ = await asyncio.wait(
                {work, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                # Even simultaneous completion cannot publish after losing the
                # lease. Await local cancellation settlement before releasing IO.
                lost_lease = True
                await heartbeat
                raise StaleRunClaim("Heartbeat stopped")
            await work
            if not started:
                raise RuntimeError("Session omitted the execution ownership barrier")
        except asyncio.CancelledError:
            error = "RunInterrupted"
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        except Exception as exc:
            error = type(exc).__name__
            deferred = isinstance(exc, SessionBusy) and not started
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            # 只记异常类型无法定位失败点；完整堆栈必须落盘可追溯
            # （WLY-20261108 第十轮二次续跑失败无法诊断）。
            logger.exception("Research command %s stopped", claim["id"])
        finally:
            # Keep renewing through graph cancellation/ledger settlement. The
            # final queue write is also fenced and can conservatively fail.
            try:
                if not lost_lease:
                    if started:
                        await self.queue.finish(
                            claim, succeeded=error is None, error=error
                        )
                    elif deferred:
                        # Report inspection/control can briefly hold the same
                        # lock. No graph action crossed the ownership barrier;
                        # wait before making this claim available again.
                        await asyncio.sleep(self.poll_interval)
                        await self.queue.defer(claim)
                    else:
                        await self.queue.reject(claim, error=error or "NotStarted")
            except Exception:
                logger.exception(
                    "Research command settlement requires recovery: %s", claim["id"]
                )
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def aclose(self, *, grace_seconds=5, cancellation_seconds=10):
        async with self._close_lock:
            if self._close_started:
                await self.drain()
                return
            self._close_started = True
            await self._close_once(grace_seconds, cancellation_seconds)

    async def _close_once(self, grace_seconds, cancellation_seconds):
        self._closing = True
        self._wake.set()
        self.session.stop_runs()
        if self._poller is not None:
            await self._poller
        tasks = set(self._tasks.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=cancellation_seconds)
        if pending:
            raise TimeoutError(
                f"Research worker shutdown timed out: {len(pending)} task(s)"
            )
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self):
        await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
