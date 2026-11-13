"""Atomic, transport-independent creation of a research session and start command."""

from hashlib import sha256

from sqlalchemy import select

from research_copilot.runtime.run_commands import parse_command
from research_copilot.runtime.run_queue import QueueConflict, RunQueue
from research_copilot.runtime.storage import sessions


async def create_start(store, record, budget):
    parse_command({"kind": "start", "payload": {"message": record.user_request}})
    queue = RunQueue(store.engine)
    job = None
    async with queue.transaction() as connection:
        existing = (
            (
                await connection.execute(
                    select(sessions).where(sessions.c.report_id == record.report_id)
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["actor_id"] != record.actor_id:
                raise LookupError("Report unavailable")
            if existing["user_request"] != record.user_request:
                raise QueueConflict("同一创建标识不能用于不同内容")
            record = store._record(existing)
        else:
            await store.create_in(
                connection, record, budget, workflow_version="products-v2"
            )
        if (
            record.status == "created"
            and not record.run_id
            and not record.cancel_requested
        ):
            identity = (
                record.report_id
                if len(record.report_id) <= 122
                else sha256(record.report_id.encode()).hexdigest()
            )
            job = await queue.enqueue_in(
                connection,
                report_id=record.report_id,
                actor_id=record.actor_id,
                operation_id="start-" + identity,
                kind="start",
                payload={"message": record.user_request},
            )
    return record, job
