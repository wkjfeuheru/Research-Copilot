"""Durable research command admission; execution is a separate integration.

All writers first lock the singleton settings row. This serializes short queue
transactions across engines/processes and makes capacity a database invariant.
Never hold that transaction while running a graph or waiting for a provider.

An expired *started* command is not replayed and keeps its admission slot: a
lease timeout cannot prove its old process or external request has stopped.
"""

from datetime import UTC, datetime, timedelta
from contextlib import asynccontextmanager
from hashlib import sha256
import json
from math import isfinite
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    case,
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    insert,
    select,
    update,
)

from research_copilot.workspace import safe_component

metadata = MetaData()
run_queue_settings = Table(
    "research_run_queue_settings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("max_active", Integer, nullable=False),
    Column("max_pending", Integer, nullable=False),
    CheckConstraint("id = 1"),
    CheckConstraint("max_active > 0 AND max_pending > 0"),
)
run_jobs = Table(
    "research_run_jobs",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("operation_id", String(128), nullable=False),
    Column("report_id", String(128), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("run_id", String(128), nullable=False),
    Column("budget_id", String(128), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("claim_token", String(32)),
    Column("worker_id", String(128)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("locked_until", DateTime(timezone=True)),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("error", String(256)),
    UniqueConstraint("actor_id", "operation_id"),
    CheckConstraint("kind IN ('start', 'resume', 'message', 'correct')"),
    CheckConstraint(
        "status IN ('pending', 'claimed', 'running', 'succeeded', 'failed', "
        "'cancelled', 'recovery_required')"
    ),
)
Index("ix_research_run_jobs_status_created", run_jobs.c.status, run_jobs.c.created_at)
Index("ix_research_run_jobs_report_status", run_jobs.c.report_id, run_jobs.c.status)

OCCUPIED = ("claimed", "running", "recovery_required")
UNFINISHED = ("pending", *OCCUPIED)


class QueueConflict(ValueError):
    pass


class QueueFull(RuntimeError):
    pass


class StaleRunClaim(RuntimeError):
    pass


async def setup(connection):
    await connection.run_sync(metadata.create_all)
    if (await connection.execute(select(run_queue_settings.c.id))).first() is None:
        await connection.execute(
            insert(run_queue_settings).values(id=1, max_active=3, max_pending=100)
        )


async def _clock(connection):
    if connection.dialect.name == "postgresql":
        return (await connection.execute(select(func.clock_timestamp()))).scalar_one()
    return datetime.now(UTC)


def _row(row):
    result = dict(row)
    for key in ("created_at", "locked_until", "started_at", "finished_at"):
        value = result.get(key)
        if value is not None and value.tzinfo is None:
            result[key] = value.replace(tzinfo=UTC)
    return result


def _lease(seconds):
    if isinstance(seconds, bool) or not isfinite(seconds) or not 1 <= seconds <= 3600:
        raise ValueError("Lease must be between 1 and 3600 seconds")
    return timedelta(seconds=seconds)


class RunQueue:
    def __init__(self, engine):
        self.engine = engine

    @asynccontextmanager
    async def transaction(self):
        """Acquire queue admission before changing session rows in the same transaction."""
        async with self.engine.begin() as connection:
            await self._guard(connection)
            yield connection

    async def _guard(self, connection):
        # An UPDATE, even without a value change, obtains a writer/row lock on
        # SQLite/PostgreSQL before any reads. No process-local lock is involved.
        row = (
            (
                await connection.execute(
                    update(run_queue_settings)
                    .where(run_queue_settings.c.id == 1)
                    .values(max_active=run_queue_settings.c.max_active)
                    .returning(run_queue_settings)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("Run queue not initialized; run rc init-db")
        return row

    async def enqueue(self, **command):
        async with self.engine.begin() as connection:
            return await self.enqueue_in(connection, **command)

    async def enqueue_in(
        self, connection, *, report_id, actor_id, operation_id, kind, payload
    ):
        """Join a caller transaction; callers must keep queue-before-session lock order.

        The envelope stores the current run/budget identity. A future executor
        must validate the command against the current checkpoint and business
        authorization; queue admission alone grants no tool/model permission.
        """
        from research_copilot.runtime.storage import sessions

        for value in (report_id, actor_id, operation_id):
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("Invalid command identity")
            safe_component(value)
        if kind not in {"start", "resume", "message", "correct"}:
            raise ValueError("Unsupported research command")
        if not isinstance(payload, dict):
            raise ValueError("Command payload must be a JSON object")
        encoded = json.dumps(
            payload, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        if len(encoded.encode()) > 65536 or json.loads(encoded) != payload:
            raise ValueError("Command payload must be bounded, lossless JSON")
        fingerprint = sha256(
            json.dumps(
                [report_id, actor_id, kind, json.loads(encoded)], sort_keys=True
            ).encode()
        ).hexdigest()
        settings = await self._guard(connection)
        report = (
            (
                await connection.execute(
                    select(sessions).where(
                        sessions.c.report_id == report_id,
                        sessions.c.actor_id == actor_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if report is None:
            raise LookupError("Report unavailable")
        if (
            report["control"].get("role_data", {}).get("workflow_version")
            != "products-v2"
        ):
            raise QueueConflict("Historical workflow is read-only; create a new report")
        existing = (
            (
                await connection.execute(
                    select(run_jobs).where(
                        run_jobs.c.actor_id == actor_id,
                        run_jobs.c.operation_id == operation_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing:
            if existing["request_hash"] != fingerprint:
                raise QueueConflict("Operation identity reused with different command")
            return _row(existing)
        if report["cancel_requested"]:
            raise QueueConflict("Cancelled sessions cannot enqueue commands")
        if (
            await connection.execute(
                select(run_jobs.c.id).where(
                    run_jobs.c.report_id == report_id, run_jobs.c.status.in_(UNFINISHED)
                )
            )
        ).first():
            raise QueueConflict("Report already has an unfinished command")
        pending = (
            await connection.execute(
                select(func.count())
                .select_from(run_jobs)
                .where(run_jobs.c.status == "pending")
            )
        ).scalar_one()
        if pending >= settings["max_pending"]:
            raise QueueFull("Research queue is full")
        return _row(
            (
                await connection.execute(
                    insert(run_jobs)
                    .values(
                        id=uuid4().hex,
                        operation_id=operation_id,
                        report_id=report_id,
                        actor_id=actor_id,
                        run_id=report["run_id"],
                        budget_id=report["budget_id"],
                        kind=kind,
                        payload=json.loads(encoded),
                        request_hash=fingerprint,
                        status="pending",
                        created_at=await _clock(connection),
                    )
                    .returning(run_jobs)
                )
            )
            .mappings()
            .one()
        )

    async def get(self, job_id, actor_id):
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(run_jobs).where(
                            run_jobs.c.id == job_id, run_jobs.c.actor_id == actor_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise LookupError("Research command unavailable")
            return _row(row)

    async def latest(self, report_id, actor_id):
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(run_jobs)
                        .where(
                            run_jobs.c.report_id == report_id,
                            run_jobs.c.actor_id == actor_id,
                        )
                        .order_by(
                            case((run_jobs.c.status.in_(UNFINISHED), 0), else_=1),
                            run_jobs.c.created_at.desc(),
                            run_jobs.c.id.desc(),
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            return _row(row) if row is not None else None

    async def active_reports(self, actor_id):
        async with self.engine.connect() as connection:
            return set(
                (
                    await connection.execute(
                        select(run_jobs.c.report_id).where(
                            run_jobs.c.actor_id == actor_id,
                            run_jobs.c.status.in_(("pending", "claimed", "running")),
                        )
                    )
                ).scalars()
            )

    async def claim(self, worker_id, *, lease_seconds=30, job_id=None):
        from research_copilot.runtime.storage import sessions

        safe_component(worker_id)
        if job_id is not None:
            safe_component(job_id)
        if len(worker_id) > 128:
            raise ValueError("Worker identity too long")
        duration = _lease(lease_seconds)
        async with self.engine.begin() as connection:
            settings = await self._guard(connection)
            now = await _clock(connection)
            await connection.execute(
                update(run_jobs)
                .where(run_jobs.c.status == "claimed", run_jobs.c.locked_until <= now)
                .values(
                    status="pending",
                    claim_token=None,
                    worker_id=None,
                    locked_until=None,
                )
            )
            await connection.execute(
                update(run_jobs)
                .where(run_jobs.c.status == "running", run_jobs.c.locked_until <= now)
                .values(status="recovery_required", error="Execution ownership expired")
            )
            active = (
                await connection.execute(
                    select(func.count())
                    .select_from(run_jobs)
                    .where(run_jobs.c.status.in_(OCCUPIED))
                )
            ).scalar_one()
            if active >= settings["max_active"]:
                return None
            waiting = (
                (
                    await connection.execute(
                        select(run_jobs)
                        .where(run_jobs.c.status == "pending")
                        .where(run_jobs.c.id == job_id if job_id is not None else True)
                        .order_by(run_jobs.c.created_at, run_jobs.c.id)
                    )
                )
                .mappings()
                .all()
            )
            for job in waiting:
                report = (
                    await connection.execute(
                        select(sessions.c.cancel_requested).where(
                            sessions.c.report_id == job["report_id"],
                            sessions.c.actor_id == job["actor_id"],
                        )
                    )
                ).first()
                if report is None or report.cancel_requested:
                    await connection.execute(
                        update(run_jobs)
                        .where(run_jobs.c.id == job["id"])
                        .values(status="cancelled", finished_at=now)
                    )
                    continue
                return _row(
                    (
                        await connection.execute(
                            update(run_jobs)
                            .where(run_jobs.c.id == job["id"])
                            .values(
                                status="claimed",
                                claim_token=uuid4().hex,
                                worker_id=worker_id,
                                locked_until=now + duration,
                            )
                            .returning(run_jobs)
                        )
                    )
                    .mappings()
                    .one()
                )
            return None

    async def _owned_update(self, claim, statuses, values):
        async with self.engine.begin() as connection:
            await self._guard(connection)
            now = await _clock(connection)
            row = (
                (
                    await connection.execute(
                        update(run_jobs)
                        .where(
                            run_jobs.c.id == claim["id"],
                            run_jobs.c.claim_token == claim["claim_token"],
                            run_jobs.c.status.in_(statuses),
                            run_jobs.c.locked_until > now,
                        )
                        .values(**values(now))
                        .returning(run_jobs)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise StaleRunClaim("Research command claim is stale")
            return _row(row)

    async def start(self, claim):
        """Must commit before invoking any graph or externally visible action."""
        return await self._owned_update(
            claim, ("claimed",), lambda now: {"status": "running", "started_at": now}
        )

    async def renew(self, claim, *, lease_seconds=30):
        duration = _lease(lease_seconds)
        return await self._owned_update(
            claim, ("claimed", "running"), lambda now: {"locked_until": now + duration}
        )

    async def defer(self, claim):
        """Return an unstarted, currently owned claim to the waiting queue."""
        return await self._owned_update(
            claim,
            ("claimed",),
            lambda now: {
                "status": "pending",
                "claim_token": None,
                "worker_id": None,
                "locked_until": None,
            },
        )

    async def finish(self, claim, *, succeeded, error=None):
        if type(succeeded) is not bool or (
            error is not None and (not isinstance(error, str) or len(error) > 256)
        ):
            raise ValueError("Invalid command completion")
        return await self._owned_update(
            claim,
            ("running",),
            lambda now: {
                "status": "succeeded" if succeeded else "failed",
                "finished_at": now,
                "locked_until": None,
                "error": error,
            },
        )

    async def reject(self, claim, *, error):
        """Reject a validated claim before execution; never releases a started run."""
        if not isinstance(error, str) or not error or len(error) > 256:
            raise ValueError("Invalid rejection reason")
        return await self._owned_update(
            claim,
            ("claimed",),
            lambda now: {
                "status": "failed",
                "finished_at": now,
                "locked_until": None,
                "error": error,
            },
        )

    async def cancel_waiting(self, report_id, actor_id):
        """Only cancel commands that have not started; running cancellation settles elsewhere."""
        async with self.engine.begin() as connection:
            await self._guard(connection)
            result = await connection.execute(
                update(run_jobs)
                .where(
                    run_jobs.c.report_id == report_id,
                    run_jobs.c.actor_id == actor_id,
                    run_jobs.c.status.in_(("pending", "claimed")),
                )
                .values(
                    status="cancelled",
                    locked_until=None,
                    finished_at=await _clock(connection),
                )
                .returning(run_jobs.c.id)
            )
            return len(result.all())
