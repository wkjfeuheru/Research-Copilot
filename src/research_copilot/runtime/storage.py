"""Durable control records. PostgreSQL production; SQLite for offline tests.

No V1 table/checkpoint is changed. setup() is an explicit application lifecycle
operation. Managed PostgreSQL report locks use the registered host connection;
raw engine stores retain the legacy lock path for component tests and callers
without a managed lifespan, not the managed host's crash-recovery guarantee.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from research_copilot.runtime.runtime_watch import RuntimeWatch
    from research_copilot.runtime.owned_report_locks import OwnedReportLocks
    from research_copilot.runtime.sandbox_slots import SandboxSlots

from sqlalchemy import (
    Boolean,
    Column,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    delete,
    inspect,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from research_copilot.contracts import RunContext
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.business import ControlState
from research_copilot.runtime.session import SessionRecord

from research_copilot.runtime.identity import (
    LEGACY_SESSION_TABLE_NAME,
    RUNTIME_VERSION,
    SESSION_TABLE_NAME,
    advisory_lock_identity,
)

metadata = MetaData()
sessions = Table(
    SESSION_TABLE_NAME,
    metadata,
    Column("report_id", String(128), primary_key=True),
    Column("actor_id", String(128), nullable=False),
    Column("runtime_version", String(16), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("budget_id", String(128), nullable=False),
    Column("run_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("cancel_requested", Boolean, nullable=False),
    Column("user_request", Text, nullable=False, server_default=""),
    Column("created_at", String(64), nullable=False, server_default=""),
    Column("control", JSON, nullable=False),
    Column("budget", JSON, nullable=False),
)


class SessionBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionSnapshot:
    record: SessionRecord
    state: ControlState
    budget: TokenBudget


class SqlSessionStore:
    def __init__(self, engine: AsyncEngine, *, resume_validator=None):
        self.runtime_watch: "RuntimeWatch | None" = None
        self.owned_report_locks: "OwnedReportLocks | None" = None
        self.sandbox_slots: "SandboxSlots | None" = None
        self.engine, self.resume_validator = engine, resume_validator
        self._local_locks: dict[str, asyncio.Lock] = {}
        self._mutation_locks: dict[str, asyncio.Lock] = {}
        # Experience tables are additive and only required by the post-delivery
        # worker. setup() enables them; read-only/legacy callers keep working.
        from research_copilot.runtime.experience import ExperienceStore

        self.experience = ExperienceStore(engine)
        self.experience_ready = False

    async def check_external_admission(self):
        if self.runtime_watch is not None:
            await self.runtime_watch.check_admission()

    async def setup(self):
        from research_copilot.runtime.migrations import migrate

        async with self.engine.begin() as connection:
            await migrate(self, connection)
        self.experience_ready = True

    async def check_schema(self):
        from research_copilot.runtime.migrations import check

        async with self.engine.connect() as connection:
            await check(connection)

    async def detect_experience(self):
        """Read-only readiness check for callers that must not run migrations."""

        names = await self._table_names()
        self.experience_ready = {
            "retrospective_jobs",
            "experiences",
            "experience_usages",
        } <= set(names)

    async def _table_names(self):
        async with self.engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )

    async def _rename_legacy_session_table(self, connection):
        names = await connection.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        if LEGACY_SESSION_TABLE_NAME in names and SESSION_TABLE_NAME not in names:
            await connection.execute(
                text(
                    f"ALTER TABLE {LEGACY_SESSION_TABLE_NAME} "
                    f"RENAME TO {SESSION_TABLE_NAME}"
                )
            )

    async def _ensure_session_columns(self, connection):
        """Add workbench columns when an existing control table predates them."""
        dialect = self.engine.dialect.name
        if dialect == "postgresql":
            await connection.execute(
                text(
                    f"ALTER TABLE {SESSION_TABLE_NAME} "
                    "ADD COLUMN IF NOT EXISTS user_request TEXT NOT NULL DEFAULT ''"
                )
            )
            await connection.execute(
                text(
                    f"ALTER TABLE {SESSION_TABLE_NAME} "
                    "ADD COLUMN IF NOT EXISTS created_at VARCHAR(64) NOT NULL DEFAULT ''"
                )
            )
            return
        if dialect != "sqlite":
            return
        names = {
            row[1]
            for row in (
                await connection.execute(
                    text(f"PRAGMA table_info({SESSION_TABLE_NAME})")
                )
            )
        }
        if "user_request" not in names:
            await connection.execute(
                text(
                    f"ALTER TABLE {SESSION_TABLE_NAME} "
                    "ADD COLUMN user_request TEXT NOT NULL DEFAULT ''"
                )
            )
        if "created_at" not in names:
            await connection.execute(
                text(
                    f"ALTER TABLE {SESSION_TABLE_NAME} "
                    "ADD COLUMN created_at VARCHAR(64) NOT NULL DEFAULT ''"
                )
            )

    async def probe(self):
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def create(
        self,
        record: SessionRecord,
        budget_snapshot: dict,
        *,
        workflow_version="products-v2",
    ):
        async with self.engine.begin() as connection:
            await self.create_in(
                connection, record, budget_snapshot, workflow_version=workflow_version
            )

    async def create_in(
        self, connection, record, budget_snapshot, *, workflow_version="products-v2"
    ):
        if (
            record.runtime_version != RUNTIME_VERSION
            or budget_snapshot["budget_id"] != record.budget_id
        ):
            raise ValueError("New records require a matching authorized budget")
        if workflow_version not in {
            None,
            "products-v1",
            "supervisor-v1",
            "products-v2",
        }:
            raise ValueError("Unknown workflow version")
        control = ControlState()
        if workflow_version:
            control.role_data["workflow_version"] = workflow_version
        await connection.execute(
            insert(sessions).values(
                **asdict(record),
                control=control.model_dump(mode="json"),
                budget=budget_snapshot,
            )
        )

    def _identity(self, report_id, actor_id):
        return (sessions.c.report_id == report_id) & (sessions.c.actor_id == actor_id)

    def _context_identity(self, context):
        return (
            self._identity(context.report_id, context.actor_id)
            & (sessions.c.runtime_version == RUNTIME_VERSION)
            & (sessions.c.policy_version == context.policy_version)
            & (sessions.c.budget_id == context.budget_id)
            & (sessions.c.run_id == context.run_id)
        )

    async def load(self, report_id, actor_id):
        return self._record(await self._load_row(report_id, actor_id))

    async def workflow_versions(self, actor_id):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(
                    sessions.c.report_id,
                    sessions.c.control["role_data"]["workflow_version"].as_string(),
                ).where(sessions.c.actor_id == actor_id)
            )
            return dict(rows.all())

    async def _load_row(self, report_id, actor_id):
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(sessions).where(self._identity(report_id, actor_id))
            )
            row = result.mappings().first()
        if row is None:
            raise LookupError("Report session is unavailable")
        return row

    async def read_snapshot(self, report_id, actor_id):
        """One authenticated row version for read-only projections; no budget writer."""
        row = await self._load_row(report_id, actor_id)
        record = self._record(row)
        if record.runtime_version != RUNTIME_VERSION:
            raise PermissionError("Report runtime is unavailable")
        budget = TokenBudget(**row["budget"])
        if budget.budget_id != record.budget_id:
            raise PermissionError("Report budget identity mismatch")
        return SessionSnapshot(
            record, ControlState.model_validate(row["control"]), budget
        )

    def _record(self, row):
        return SessionRecord(
            **{key: row[key] for key in SessionRecord.__dataclass_fields__}
        )

    async def list_for_actor(self, actor_id: str) -> list[SessionRecord]:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(*(sessions.c[key] for key in SessionRecord.__dataclass_fields__))
                .where(sessions.c.actor_id == actor_id)
                .order_by(sessions.c.created_at.desc(), sessions.c.report_id.desc())
            )
            rows = result.mappings().all()
        return [self._record(row) for row in rows]

    async def delete_if_idle(self, report_id: str, actor_id: str) -> None:
        from research_copilot.runtime.run_queue import RunQueue, run_jobs, UNFINISHED

        async with RunQueue(self.engine).transaction() as connection:
            row = (
                (
                    await connection.execute(
                        select(sessions.c.status).where(
                            self._identity(report_id, actor_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LookupError("Report session is unavailable")
            queued = (
                await connection.execute(
                    select(run_jobs.c.id).where(
                        run_jobs.c.report_id == report_id,
                        run_jobs.c.actor_id == actor_id,
                        run_jobs.c.status.in_(UNFINISHED),
                    )
                )
            ).first()
            if queued:
                raise SessionBusy("Report has an unfinished command")
            if row["status"] == "running":
                raise SessionBusy("Report is already running")
            result = await connection.execute(
                delete(sessions).where(
                    self._identity(report_id, actor_id)
                    & (sessions.c.status != "running")
                )
            )
            if result.rowcount != 1:
                raise SessionBusy("Report is already running")

    async def save(self, record):
        values = {
            "run_id": record.run_id,
            "status": record.status,
            "cancel_requested": or_(
                sessions.c.cancel_requested, record.cancel_requested
            ),
        }
        where = (
            self._identity(record.report_id, record.actor_id)
            & (sessions.c.runtime_version == record.runtime_version)
            & (sessions.c.policy_version == record.policy_version)
            & (sessions.c.budget_id == record.budget_id)
        )
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(sessions).where(where).values(**values)
            )
            if result.rowcount != 1:
                raise LookupError("Session identity or policy changed")

    async def request_cancel(self, report_id, actor_id):
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(sessions)
                .where(self._identity(report_id, actor_id))
                .values(cancel_requested=True)
            )
            if result.rowcount != 1:
                raise LookupError("Report session is unavailable")

    @asynccontextmanager
    async def lock(self, report_id):
        if self.owned_report_locks is not None:
            async with self.owned_report_locks.hold(report_id):
                yield
            return
        if self.engine.dialect.name == "sqlite":
            # SQLite is an offline test backend; production uses the database lock below.
            lock = self._local_locks.setdefault(report_id, asyncio.Lock())
            if lock.locked():
                raise SessionBusy("Report is already running")
            async with lock:
                yield
            return
        if self.engine.dialect.name != "postgresql":
            raise ValueError("Production sessions require PostgreSQL")
        key = advisory_lock_identity(report_id)
        async with self.engine.connect() as connection:
            acquired = (
                await connection.execute(
                    text("select pg_try_advisory_lock(:key)"), {"key": key}
                )
            ).scalar()
            await connection.commit()
            if not acquired:
                raise SessionBusy("Report is already running")
            try:
                yield
            finally:
                await connection.execute(
                    text("select pg_advisory_unlock(:key)"), {"key": key}
                )
                await connection.commit()

    async def validate_resume(self, record, actor_id, pending, decisions):
        if self.resume_validator is None:
            raise RuntimeError("A business resume validator must be configured")
        if record.actor_id != actor_id:
            raise PermissionError("Reviewer identity mismatch")
        context = RunContext(
            actor_id,
            record.report_id,
            record.run_id,
            record.budget_id,
            record.policy_version,
        )
        await self.resume_validator(context, pending, decisions)

    async def read_control(self, context):
        # A sibling review failure can cancel this short read while the driver is
        # executing. Let its owning task release the connection before propagating
        # cancellation; otherwise SQLite's graceful-close task can outlive the loop.
        pending_read = asyncio.create_task(self._read_control(context))
        try:
            return await asyncio.shield(pending_read)
        except asyncio.CancelledError:
            try:
                await pending_read
            finally:
                raise

    async def _read_control(self, context):
        async with self.engine.connect() as connection:
            value = (
                await connection.execute(
                    select(sessions.c.control).where(self._context_identity(context))
                )
            ).scalar_one()
            return ControlState.model_validate(value)

    async def mutate_control(
        self,
        context,
        operation,
        *,
        effects=(),
        include_budget=False,
        enqueue_command=None,
    ):
        if effects and not self.experience_ready:
            raise RuntimeError("Experience tables are not initialized")
        if self.engine.dialect.name == "sqlite":
            # SQLite ignores SELECT FOR UPDATE. Keep offline parallel branch writes
            # serial, matching PostgreSQL's row-lock behavior inside a running report.
            async with self._mutation_locks.setdefault(
                context.report_id, asyncio.Lock()
            ):
                return await self._mutate_control(
                    context, operation, effects, include_budget, enqueue_command
                )
        return await self._mutate_control(
            context, operation, effects, include_budget, enqueue_command
        )

    async def _mutate_control(
        self, context, operation, effects=(), include_budget=False, enqueue_command=None
    ):
        from research_copilot.runtime.run_queue import RunQueue

        queue = RunQueue(self.engine)
        transaction = (
            queue.transaction() if enqueue_command is not None else self.engine.begin()
        )
        async with transaction as connection:
            value = (
                await connection.execute(
                    select(sessions.c.control, sessions.c.budget)
                    .where(self._context_identity(context))
                    .with_for_update()
                )
            ).one()
            state = ControlState.model_validate(value.control)
            result = (
                operation(state, TokenBudget(**value.budget).status())
                if include_budget
                else operation(state)
            )
            await connection.execute(
                update(sessions)
                .where(self._context_identity(context))
                .values(control=state.model_dump(mode="json"))
            )
            if effects:
                if not self.experience_ready:
                    raise RuntimeError("Experience tables are not initialized")
                from research_copilot.runtime.experience import apply_effect

                for effect in effects:
                    await apply_effect(connection, context, effect)
            if enqueue_command is not None:
                await queue.enqueue_in(
                    connection,
                    report_id=context.report_id,
                    actor_id=context.actor_id,
                    **enqueue_command(result),
                )
            return result

    async def load_budget(self, context):
        async with self.engine.connect() as connection:
            snapshot = (
                await connection.execute(
                    select(sessions.c.budget).where(self._context_identity(context))
                )
            ).scalar_one()

        async def persist(value):
            async with self.engine.begin() as connection:
                current = (
                    await connection.execute(
                        select(sessions.c.budget)
                        .where(self._context_identity(context))
                        .with_for_update()
                    )
                ).scalar_one()
                if (
                    value["limit"] != current["limit"]
                    or value["budget_id"] != current["budget_id"]
                    or value.get("call_limit") != current.get("call_limit")
                    or value.get("stage_limits", {}) != current.get("stage_limits", {})
                    or value.get("prices", {}) != current.get("prices", {})
                ):
                    raise ValueError("Resuming cannot increase the authorized budget")
                if value.get("revision", 0) != current.get("revision", 0) + 1:
                    raise ValueError("Stale budget ledger revision")
                from research_copilot.runtime.budget_persistence import (
                    validate_reconciliation,
                )

                await validate_reconciliation(current, value)
                await connection.execute(
                    update(sessions)
                    .where(self._context_identity(context))
                    .values(budget=value)
                )

        return TokenBudget(**snapshot, persist=persist)

    async def extend_stage_budget(
        self, context, *, stage, tokens=None, calls=None, actor_id, note,
        branch=None, branch_calls=None,
    ):
        """Operator-audited stage cap extension; never sends a model request."""
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Operator budget adjustment requires a nonempty note")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ValueError("Operator budget adjustment requires an actor")
        if tokens is None and calls is None and branch_calls is None:
            raise ValueError("Stage adjustment requires a token or call limit")
        if tokens is not None and (type(tokens) is not int or tokens <= 0):
            raise ValueError("Stage token limit must be a positive integer")
        if calls is not None and (type(calls) is not int or calls <= 0):
            raise ValueError("Stage call limit must be a positive integer")
        if branch_calls is not None and (type(branch_calls) is not int or branch_calls <= 0):
            raise ValueError("Branch call limit must be a positive integer")
        if branch_calls is not None and branch is None:
            raise ValueError("Branch adjustment requires a branch name")
        if self.engine.dialect.name == "sqlite":
            async with self._mutation_locks.setdefault(
                context.report_id, asyncio.Lock()
            ):
                return await self._extend_stage_budget(
                    context,
                    stage=stage,
                    tokens=tokens,
                    calls=calls,
                    actor_id=actor_id,
                    note=note,
                    branch=branch,
                    branch_calls=branch_calls,
                )
        return await self._extend_stage_budget(
            context,
            stage=stage,
            tokens=tokens,
            calls=calls,
            actor_id=actor_id,
            note=note,
            branch=branch,
            branch_calls=branch_calls,
        )

    async def _extend_stage_budget(
        self, context, *, stage, tokens, calls, actor_id, note,
        branch=None, branch_calls=None,
    ):
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(sessions.c.control, sessions.c.budget)
                    .where(self._context_identity(context))
                    .with_for_update()
                )
            ).one()
            state = ControlState.model_validate(row.control)
            budget = deepcopy(row.budget)
            caps = budget.get("stage_limits") or {}
            if stage not in caps:
                raise ValueError("Unknown budget stage: " + str(stage))
            current_tokens = caps[stage].get("tokens")
            if type(current_tokens) is not int or current_tokens <= 0:
                raise ValueError("Stage token limit is missing")
            if tokens is not None:
                if tokens <= current_tokens:
                    raise ValueError("Stage token limit can only be extended")
                if tokens > budget["limit"]:
                    raise ValueError(
                        "Stage token limit cannot exceed the global token limit"
                    )
            current_calls = caps[stage].get("calls")
            if calls is not None:
                if type(current_calls) is not int or current_calls <= 0:
                    raise ValueError("Stage call limit is missing")
                if calls <= current_calls:
                    raise ValueError("Stage call limit can only be extended")
                if budget.get("call_limit") and calls > budget["call_limit"]:
                    raise ValueError(
                        "Stage call limit cannot exceed the global call limit"
                    )
            current_branch_calls = None
            if branch_calls is not None:
                branches = caps[stage].get("branches") or {}
                if branch not in branches:
                    raise ValueError("Unknown review branch: " + str(branch))
                current_branch_calls = branches[branch]
                if type(current_branch_calls) is not int or current_branch_calls <= 0:
                    raise ValueError("Branch call limit is missing")
                if branch_calls <= current_branch_calls:
                    raise ValueError("Branch call limit can only be extended")
                effective_stage_calls = calls if calls is not None else current_calls
                if branch_calls > effective_stage_calls:
                    raise ValueError(
                        "Branch call limit cannot exceed the stage call limit"
                    )
            pending = sum((budget.get("pending") or {}).values())
            if budget.get("charged_tokens", 0) + pending >= budget["limit"]:
                raise ValueError(
                    "Global token budget is exhausted; extending a stage cannot help"
                )
            if (
                (calls is not None or branch_calls is not None)
                and budget.get("call_limit")
                and budget.get("calls", 0) >= budget["call_limit"]
            ):
                raise ValueError(
                    "Global model call budget is exhausted; extending a stage cannot help"
                )
            if tokens is not None:
                caps[stage]["tokens"] = tokens
            if calls is not None:
                caps[stage]["calls"] = calls
            if branch_calls is not None:
                caps[stage]["branches"][branch] = branch_calls
            # A later load_budget persist expects an incrementing revision.
            budget["revision"] = budget.get("revision", 0) + 1
            receipt_id = "operator:budget:" + uuid4().hex
            repaired = []
            for ident, failure in state.role_data.get("dispatch_failures", {}).items():
                dispatch = state.dispatches.get(ident) or {}
                if (
                    failure.get("code") == "stage_budget_exhausted"
                    and dispatch.get("role") == stage
                    and not failure.get("repaired_by")
                ):
                    failure["repaired_by"] = receipt_id
                    repaired.append(ident)
            entry = {
                "receipt_id": receipt_id,
                "stage": stage,
                "previous_tokens": current_tokens,
                "previous_calls": current_calls,
                "actor_id": actor_id,
                "note": note,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "repaired_dispatches": repaired,
            }
            if tokens is not None:
                entry["tokens"] = tokens
            if calls is not None:
                entry["calls"] = calls
            if branch_calls is not None:
                entry["branch"] = branch
                entry["previous_branch_calls"] = current_branch_calls
                entry["branch_calls"] = branch_calls
            state.role_data.setdefault("budget_adjustments", []).append(entry)
            await connection.execute(
                update(sessions)
                .where(self._context_identity(context))
                .values(
                    control=state.model_dump(mode="json"),
                    budget=budget,
                )
            )
            return {
                **entry,
                "budget": TokenBudget(**budget).status(),
            }

    async def extend_global_budget(
        self, context, *, tokens=None, calls=None, actor_id, note
    ):
        """Operator-audited global cap extension; never sends a model request."""
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Operator budget adjustment requires a nonempty note")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ValueError("Operator budget adjustment requires an actor")
        if tokens is None and calls is None:
            raise ValueError("Global adjustment requires a token or call limit")
        if tokens is not None and (type(tokens) is not int or tokens <= 0):
            raise ValueError("Global token limit must be a positive integer")
        if calls is not None and (type(calls) is not int or calls <= 0):
            raise ValueError("Global call limit must be a positive integer")
        if self.engine.dialect.name == "sqlite":
            async with self._mutation_locks.setdefault(
                context.report_id, asyncio.Lock()
            ):
                return await self._extend_global_budget(
                    context,
                    tokens=tokens,
                    calls=calls,
                    actor_id=actor_id,
                    note=note,
                )
        return await self._extend_global_budget(
            context,
            tokens=tokens,
            calls=calls,
            actor_id=actor_id,
            note=note,
        )

    async def _extend_global_budget(
        self, context, *, tokens, calls, actor_id, note
    ):
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(sessions.c.control, sessions.c.budget)
                    .where(self._context_identity(context))
                    .with_for_update()
                )
            ).one()
            state = ControlState.model_validate(row.control)
            budget = deepcopy(row.budget)
            current_tokens = budget.get("limit")
            if type(current_tokens) is not int or current_tokens <= 0:
                raise ValueError("Global token limit is missing")
            current_calls = budget.get("call_limit")
            if tokens is not None:
                if tokens <= current_tokens:
                    raise ValueError("Global token limit can only be extended")
            if calls is not None:
                if current_calls is None:
                    raise ValueError("Global call limit is missing")
                if calls <= current_calls:
                    raise ValueError("Global call limit can only be extended")
            for stage, caps in (budget.get("stage_limits") or {}).items():
                if tokens is not None and caps.get("tokens") and caps["tokens"] > tokens:
                    raise ValueError(
                        "Global token limit cannot fall below stage limit: " + stage
                    )
                if (
                    calls is not None
                    and caps.get("calls")
                    and calls < caps["calls"]
                ):
                    raise ValueError(
                        "Global call limit cannot fall below stage limit: " + stage
                    )
            if tokens is not None:
                budget["limit"] = tokens
            if calls is not None:
                budget["call_limit"] = calls
            # A later load_budget persist expects an incrementing revision.
            budget["revision"] = budget.get("revision", 0) + 1
            receipt_id = "operator:budget:" + uuid4().hex
            repaired = []
            for ident, failure in state.role_data.get("dispatch_failures", {}).items():
                if (
                    failure.get("code")
                    in {"stage_budget_exhausted", "task_token_budget_exhausted"}
                    and not failure.get("repaired_by")
                ):
                    failure["repaired_by"] = receipt_id
                    repaired.append(ident)
            entry = {
                "receipt_id": receipt_id,
                "stage": None,
                "previous_tokens": current_tokens,
                "previous_calls": current_calls,
                "actor_id": actor_id,
                "note": note,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "repaired_dispatches": repaired,
            }
            if tokens is not None:
                entry["tokens"] = tokens
            if calls is not None:
                entry["calls"] = calls
            state.role_data.setdefault("budget_adjustments", []).append(entry)
            await connection.execute(
                update(sessions)
                .where(self._context_identity(context))
                .values(
                    control=state.model_dump(mode="json"),
                    budget=budget,
                )
            )
            return {
                **entry,
                "budget": TokenBudget(**budget).status(),
            }


@asynccontextmanager
async def postgres_runtime(
    database_url: str,
    *,
    initialize: bool = False,
    maintenance: bool = False,
    read_only: bool = False,
):
    """Own the database access lock until all runtime dependencies have closed.

    Initialization and explicit offline configuration changes require exclusive
    maintenance access. Ordinary readers/writers share access, excluding DDL and
    backup/restore without changing application rows for read-only commands.
    """
    from research_copilot.runtime.maintenance import async_database_access

    if read_only and (initialize or maintenance):
        raise ValueError("Read-only access cannot initialize or perform maintenance")
    async with async_database_access(
        database_url,
        exclusive=initialize or maintenance,
        operation="initialize" if initialize else "bind_host" if maintenance else None,
    ) as connection:
        async with _postgres_runtime(
            database_url,
            initialize=initialize,
            read_only=read_only,
            owner_connection=None if read_only else connection,
        ) as runtime:
            yield runtime


@asynccontextmanager
async def _postgres_runtime(
    database_url: str,
    *,
    initialize: bool = False,
    read_only=False,
    owner_connection=None,
):
    """Own database/checkpoint connections for the application's lifespan.

    initialize explicitly provisions only control, additive experience and
    LangGraph checkpoint tables. Calling/importing otherwise performs no schema
    migration; a read-only table probe enables experience features when the
    tables already exist.
    https://docs.langchain.com/oss/python/langgraph/persistence
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg import AsyncConnection
    from typing import Any
    from psycopg_pool import AsyncConnectionPool
    from sqlalchemy.ext.asyncio import create_async_engine
    from research_copilot.runtime.urls import to_checkpoint_url, to_sqlalchemy_url
    from research_copilot.runtime.checkpoint import checkpoint_serializer
    from research_copilot.runtime.runtime_ownership import register_owner, close_owner
    from research_copilot.runtime.runtime_watch import watch_owner
    from research_copilot.runtime.owned_report_locks import OwnedReportLocks
    from research_copilot.runtime.maintenance import MaintenanceLost

    engine = create_async_engine(to_sqlalchemy_url(database_url), pool_pre_ping=True)
    if read_only:
        engine = engine.execution_options(postgresql_readonly=True)

    async def configure(connection):
        if read_only:
            await connection.execute("SET default_transaction_read_only=on")

    # A single long-lived connection never recovers after PostgreSQL restarts, so
    # the first checkpoint read fails with a psycopg OperationalError. The pool
    # validates connections on checkout and silently replaces dead ones.
    pool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
        to_checkpoint_url(database_url),
        min_size=1,
        max_size=4,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "application_name": "research-copilot-checkpoint",
        },
        check=AsyncConnectionPool.check_connection,
        configure=configure,
    )
    owner = None
    watch = None
    report_locks = None
    failed = False
    try:
        await pool.open(wait=True)
        checkpointer = AsyncPostgresSaver(conn=pool, serde=checkpoint_serializer())
        store = SqlSessionStore(engine)
        if initialize:
            await store.setup()
            await checkpointer.setup()
        else:
            await store.check_schema()
            await store.detect_experience()
        if owner_connection is not None:
            owner = await register_owner(owner_connection)
            watch = watch_owner(owner_connection, owner)
            store.runtime_watch = watch
            from research_copilot.runtime.sandbox_slots import SandboxSlots

            store.sandbox_slots = SandboxSlots(engine, owner)
            report_locks = OwnedReportLocks(owner_connection, watch)
            store.owned_report_locks = report_locks
            watch.start()
        yield store, checkpointer
    except BaseException:
        failed = True
        raise
    finally:
        if watch is not None:
            await watch.close()
        if report_locks is not None:
            await report_locks.close()
        try:
            await pool.close()
        finally:
            await engine.dispose()
        # Only mark closed after both dependencies drained. A failed owning
        # connection must leave its row open, never reconnect to clear it.
        if owner is not None:
            try:
                if watch is not None and watch.lost.is_set():
                    raise MaintenanceLost(
                        "Runtime ownership lost; preserved owner requires explicit recovery"
                    )
                await close_owner(owner_connection, owner)
            except Exception:
                if not failed:
                    raise
                import logging

                logging.getLogger(__name__).error(
                    "Runtime ownership remains unsettled after failed operation"
                )
