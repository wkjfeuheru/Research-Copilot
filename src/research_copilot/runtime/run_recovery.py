"""Audited manual release of lost execution ownership; never resumes a graph."""

from hashlib import sha256
import json
from typing import Literal

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy import insert, select, update
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.run_queue import (
    QueueConflict,
    RunQueue,
    _clock,
    _row,
    run_jobs,
)
from research_copilot.runtime.storage import sessions

metadata = MetaData()
run_recoveries = Table(
    "research_run_recoveries",
    metadata,
    Column("job_id", String(32), primary_key=True),
    Column("report_id", String(128), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("claim_token", String(32), nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("run_id", String(128), nullable=False),
    Column("budget_id", String(128), nullable=False),
    Column("budget_revision", Integer, nullable=False),
    Column("unresolved_requests", JSON, nullable=False),
    Column("note", Text, nullable=False),
    Column("owner_stopped", Boolean, nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _view(row):
    value = _row(row)
    value["created_at"] = value["created_at"].isoformat()
    return value


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claim_token: str = Field(min_length=1, max_length=32)
    owner_stopped: StrictBool
    note: str = Field(min_length=1, max_length=2000)


class RequestRecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["resolve_pending", "reconcile", "authorize_retry"]
    expected_revision: StrictInt = Field(ge=0)
    owner_stopped: StrictBool
    note: str = Field(min_length=1, max_length=2000)
    usage: StrictInt | None = Field(default=None, ge=0)
    acknowledge_duplicate_charge: StrictBool = False

    @model_validator(mode="after")
    def separate_decisions(self):
        if self.action == "authorize_retry" and self.usage is not None:
            raise ValueError("Retry authorization cannot reconcile usage")
        if self.action != "authorize_retry" and self.acknowledge_duplicate_charge:
            raise ValueError("Usage recovery cannot authorize retry")
        return self


def _unresolved(budget):
    return sorted(
        key
        for key, request in budget.requests.items()
        if not request.get("released")
        and (
            request.get("usage") is None
            or request.get("details", {}).get("usage_provisional")
        )
    )


class RunRecovery:
    def __init__(self, store):
        self.store = store
        self.queue = RunQueue(store.engine)

    async def inspect(self, report_id, actor_id):
        async with self.store.engine.connect() as connection:
            record = (
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
            if record is None:
                raise LookupError("Unknown report")
            budget = TokenBudget(**record["budget"])
            now = await _clock(connection)
        job = await self.queue.latest(report_id, actor_id)
        receipt = None
        if job is not None:
            async with self.store.engine.connect() as connection:
                receipt = (
                    (
                        await connection.execute(
                            select(run_recoveries).where(
                                run_recoveries.c.job_id == job["id"],
                                run_recoveries.c.actor_id == actor_id,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        pending = sorted(budget.snapshot()["pending"])
        return {
            "command": {
                key: job[key]
                for key in (
                    "id",
                    "kind",
                    "status",
                    "claim_token",
                    "worker_id",
                    "locked_until",
                    "started_at",
                    "run_id",
                    "budget_id",
                )
            }
            if job
            else None,
            "pending_requests": pending,
            "unresolved_requests": _unresolved(budget),
            "budget_revision": budget.revision,
            "budget": budget.status(),
            "requests": [
                {
                    "id": key,
                    "pending": key in pending,
                    **{
                        name: item.get(name)
                        for name in (
                            "stage",
                            "sent",
                            "reserved",
                            "usage",
                            "released",
                            "operation_id",
                            "recovery",
                            "reconcile_details",
                            "reconciled",
                            "retry_authorization",
                        )
                    },
                    "usage_provisional": bool(
                        item.get("details", {}).get("usage_provisional")
                    ),
                }
                for key, item in budget.requests.items()
            ],
            "can_release": bool(
                job
                and job["status"] in {"running", "recovery_required"}
                and job["locked_until"]
                and job["locked_until"] <= now
                and not pending
            ),
            "receipt": _view(receipt) if receipt else None,
        }

    async def release(
        self, *, report_id, actor_id, job_id, claim_token, owner_stopped, note
    ):
        if (
            owner_stopped is not True
            or not isinstance(note, str)
            or not 1 <= len(note.strip()) <= 2000
        ):
            raise ValueError(
                "Confirm the old owner/executions stopped and provide evidence (1-2000 characters)"
            )
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("The observed claim token is required")
        note = note.strip()
        request_hash = sha256(
            json.dumps(
                [report_id, actor_id, job_id, claim_token, note], ensure_ascii=False
            ).encode()
        ).hexdigest()
        # A timeout alone cannot establish process termination. The caller must
        # attest that separately, and a still-held report lock always vetoes it.
        async with self.store.lock(report_id):
            async with self.queue.transaction() as connection:
                job = (
                    (
                        await connection.execute(
                            select(run_jobs).where(
                                run_jobs.c.id == job_id,
                                run_jobs.c.report_id == report_id,
                                run_jobs.c.actor_id == actor_id,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if job is None:
                    raise LookupError("Unknown research command")
                previous = (
                    (
                        await connection.execute(
                            select(run_recoveries).where(
                                run_recoveries.c.job_id == job_id
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if previous is not None:
                    if previous["request_hash"] != request_hash:
                        raise QueueConflict(
                            "Recovery already recorded with different evidence"
                        )
                    return _view(previous)
                job = _row(job)
                now = await _clock(connection)
                if (
                    job["claim_token"] != claim_token
                    or job["status"] not in {"running", "recovery_required"}
                    or job["locked_until"] is None
                    or job["locked_until"] > now
                ):
                    raise QueueConflict(
                        "Command is not an expired execution with this owner"
                    )
                record = (
                    (
                        await connection.execute(
                            select(sessions)
                            .where(
                                sessions.c.report_id == report_id,
                                sessions.c.actor_id == actor_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    raise LookupError("Unknown report")
                if record["budget_id"] != job["budget_id"] or (
                    job["run_id"] and record["run_id"] != job["run_id"]
                ):
                    raise QueueConflict("Command run/budget identity changed")
                budget = TokenBudget(**record["budget"])
                if budget.snapshot()["pending"]:
                    raise QueueConflict(
                        "Resolve pending requests before releasing execution ownership"
                    )
                unresolved = _unresolved(budget)
                receipt = {
                    "job_id": job_id,
                    "report_id": report_id,
                    "actor_id": actor_id,
                    "claim_token": claim_token,
                    "worker_id": job["worker_id"],
                    "run_id": record["run_id"],
                    "budget_id": record["budget_id"],
                    "budget_revision": budget.revision,
                    "unresolved_requests": unresolved,
                    "note": note,
                    "owner_stopped": True,
                    "request_hash": request_hash,
                    "created_at": now,
                }
                await connection.execute(
                    update(run_jobs)
                    .where(run_jobs.c.id == job_id)
                    .values(
                        status="failed",
                        finished_at=now,
                        locked_until=None,
                        error="Execution ownership released after operator verification",
                    )
                )
                await connection.execute(insert(run_recoveries).values(**receipt))
                return _view(receipt)
