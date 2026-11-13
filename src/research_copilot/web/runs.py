"""Web admission and lifecycle for durable research commands."""

import asyncio
from uuid import uuid4

from fastapi import HTTPException

from research_copilot.runtime.run_commands import parse_command
from research_copilot.runtime.run_queue import RunQueue
from research_copilot.runtime.run_worker import RunWorker
from research_copilot.runtime.run_admission import create_start


class DurableRuns:
    def __init__(self, session, store, actor_id):
        self.session, self.store, self.actor_id = session, store, actor_id
        self.queue = RunQueue(store.engine)
        self.worker = RunWorker(self.queue, session)
        self.errors = {}  # Read-only graph inspection errors; execution errors are durable.
        self._closing = False
        self._admission = asyncio.Lock()

    def accepting(self):
        watch = getattr(self.store, "runtime_watch", None)
        if self._closing or (watch is not None and not watch.available):
            raise HTTPException(503, "服务正在关闭，暂不接受新任务")

    async def start(self):
        self.accepting()
        await self.worker.start()

    async def state(self, report_id):
        return await self.queue.latest(report_id, self.actor_id)

    async def busy(self, report_id):
        job = await self.state(report_id)
        return job is not None and job["status"] in {"pending", "claimed", "running"}

    async def create(self, record, budget):
        async with self._admission:
            self.accepting()
            if record.actor_id != self.actor_id:
                raise ValueError("Actor identity mismatch")
            record, _ = await create_start(self.store, record, budget)
            await self.start()
            return record

    async def submit(self, report_id, kind, payload, *, operation_id=None):
        async with self._admission:
            self.accepting()
            parse_command({"kind": kind, "payload": payload})
            job = await self.queue.enqueue(
                report_id=report_id,
                actor_id=self.actor_id,
                operation_id=operation_id or uuid4().hex,
                kind=kind,
                payload=payload,
            )
            self.errors.pop(report_id, None)
            await self.start()
            return job

    async def correct(self, report_id, dispatch_call_id):
        async with self._admission:
            self.accepting()
            authorization_id = await self.session.authorize_correction(
                report_id,
                self.actor_id,
                dispatch_call_id,
                enqueue=True,
            )
            await self.start()
            return authorization_id

    async def cancel_waiting(self, report_id):
        return await self.queue.cancel_waiting(report_id, self.actor_id)

    async def release(self, report_id, job_id, body):
        from research_copilot.runtime.run_recovery import RunRecovery

        async with self._admission:
            self.accepting()
            return await RunRecovery(self.store).release(
                report_id=report_id,
                actor_id=self.actor_id,
                job_id=job_id,
                **body.model_dump(),
            )

    async def recover_request(self, report_id, request_id, body):
        from research_copilot.runtime.request_recovery import recover_request

        async with self._admission:
            self.accepting()
            if not body.owner_stopped:
                raise ValueError(
                    "Confirm the old owner and its executions have stopped"
                )
            retry = body.action == "authorize_retry"
            if retry and not body.acknowledge_duplicate_charge:
                raise ValueError(
                    "Acknowledge possible duplicate charges before authorizing retry"
                )
            return await recover_request(
                self.store,
                report_id=report_id,
                actor_id=self.actor_id,
                request_id=request_id,
                note=body.note,
                usage=body.usage,
                authorize_retry=retry,
                expected_revision=body.expected_revision,
                action=body.action,
            )

    async def aclose(self, *, grace_seconds=5, cancellation_seconds=10):
        async with self._admission:
            self._closing = True
        await self.worker.aclose(
            grace_seconds=grace_seconds, cancellation_seconds=cancellation_seconds
        )

    async def drain(self):
        if self._closing:
            await self.worker.drain()
            return
        while await self.queue.active_reports(self.actor_id) or self.worker._tasks:
            await self.worker.drain()
            await asyncio.sleep(0.01)
