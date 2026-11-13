"""Disposable databases only. Explicit -m db; never drops the supplied database."""

import asyncio
import os
import shutil
from typing import TypedDict
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from langgraph.graph import StateGraph, START, END

from research_copilot.runtime.backup import backup, restore
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.run_queue import RunQueue
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.host_binding import (
    bind_host,
    check_host,
    digest,
    MARKER,
)
from research_copilot.runtime.urls import to_checkpoint_url
from tests.unit.test_orchestration import context

pytestmark = pytest.mark.db


class SavedState(TypedDict):
    step: int


async def test_joint_restore_preserves_pending_budget_control_checkpoint_and_files(
    tmp_path,
):
    base = os.environ.get("RC_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Requires explicit isolated RC_TEST_DATABASE_URL")
    assert shutil.which("pg_dump") and shutil.which("pg_restore"), (
        "PostgreSQL backup client tools required"
    )
    base = to_checkpoint_url(base)
    names = ["rc_restore_" + uuid4().hex for _ in range(2)]
    params = psycopg.conninfo.conninfo_to_dict(base)
    # Runtime URL adapters require URI; build it without printing credentials.
    from sqlalchemy.engine import URL

    urls = [
        URL.create(
            "postgresql",
            username=params.get("user"),
            password=params.get("password"),
            host=params.get("host"),
            port=int(params.get("port", 5432)),
            database=name,
        ).render_as_string(hide_password=False)
        for name in names
    ]
    created = []
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        try:
            for name in names:
                await admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                )
                created.append(name)
            workspace = tmp_path / "source-workspace"
            (workspace / "report-1").mkdir(parents=True)
            (workspace / "report-1/report.md").write_text(
                "Frozen artifact 中文", encoding="utf-8"
            )
            graph = StateGraph(SavedState)
            graph.add_node("step", lambda state: {"step": state["step"] + 1})
            graph.add_edge(START, "step")
            graph.add_edge("step", END)
            config = {"configurable": {"thread_id": "v2:report-1"}}
            async with postgres_runtime(urls[0], initialize=True) as (store, saver):
                fingerprint = digest({"models": "offline-restore-test"})
                original_binding = await bind_host(
                    store,
                    workspace,
                    fingerprint,
                    actor_id="operator",
                    note="Isolated source before adding pending work",
                )
                await store.create(
                    SessionRecord(
                        "report-1",
                        "actor-1",
                        "v2",
                        "policy-1",
                        "budget-1",
                        run_id="run-1",
                    ),
                    TokenBudget(limit=1000, budget_id="budget-1").snapshot(),
                    workflow_version="products-v1",
                )
                budget = await store.load_budget(context())
                from research_copilot.runtime.run_recovery import RunRecovery
                from tests.integration.test_run_recovery import (
                    lost_command,
                    release_args,
                )

                lost = await lost_command(store)
                recovery_receipt = await RunRecovery(store).release(
                    **release_args(lost)
                )
                request = await budget.reserve(100, operation_id="crashed-node")
                await budget.mark_sent(request)
                frozen_budget = budget.snapshot()
                await store.mutate_control(
                    context(),
                    lambda state: state.role_data.update(restore_probe="same-control"),
                )
                await graph.compile(checkpointer=saver).ainvoke({"step": 0}, config)
                queued = await RunQueue(store.engine).enqueue(
                    report_id="report-1",
                    actor_id="actor-1",
                    operation_id=uuid4().hex,
                    kind="resume",
                    payload={"decision": None},
                )
            # Build the supported unversioned legacy shape in this disposable DB.
            # Preserve real session/control/budget/checkpoint rows through upgrade.
            async with await psycopg.AsyncConnection.connect(
                urls[0], autocommit=True
            ) as legacy:
                await legacy.execute("ALTER TABLE sessions RENAME TO rc_v2_sessions")
                await legacy.execute(
                    "ALTER TABLE rc_v2_sessions DROP COLUMN user_request"
                )
                await legacy.execute(
                    "ALTER TABLE rc_v2_sessions DROP COLUMN created_at"
                )
                await legacy.execute("DROP TABLE rc_schema_migrations")
            with pytest.raises(ValueError, match="init-db"):
                async with postgres_runtime(urls[0]):
                    pytest.fail("Startup silently migrated the legacy database")
            async with await psycopg.AsyncConnection.connect(urls[0]) as legacy:
                cursor = await legacy.execute(
                    "SELECT to_regclass('sessions'), to_regclass('rc_v2_sessions')"
                )
                assert await cursor.fetchone() == (None, "rc_v2_sessions")
            async with postgres_runtime(urls[0], initialize=True) as (store, saver):
                assert (await store.load_budget(context())).snapshot() == frozen_budget
                assert (await store.read_control(context())).role_data[
                    "restore_probe"
                ] == "same-control"
                assert (
                    await graph.compile(checkpointer=saver).aget_state(config)
                ).values["step"] == 1
            bundle = tmp_path / "bundle"
            await asyncio.to_thread(backup, urls[0], workspace, bundle)
            destination = tmp_path / "restored-workspace"
            async with await psycopg.AsyncConnection.connect(
                urls[1], autocommit=True
            ) as target:
                await target.execute("CREATE SCHEMA pgcustom")
                with pytest.raises(ValueError, match="empty dedicated database"):
                    await asyncio.to_thread(restore, urls[1], bundle, destination)
                assert not destination.exists()
                await target.execute("DROP SCHEMA pgcustom")
                await target.execute(
                    "CREATE VIEW public.must_preserve AS SELECT 1 AS n"
                )
                with pytest.raises(ValueError, match="empty dedicated database"):
                    await asyncio.to_thread(restore, urls[1], bundle, destination)
                assert not destination.exists()
                assert (
                    await (
                        await target.execute("SELECT n FROM public.must_preserve")
                    ).fetchone()
                )[0] == 1
                await target.execute("DROP VIEW public.must_preserve")
            await asyncio.to_thread(restore, urls[1], bundle, destination)
            async with postgres_runtime(urls[1]) as (store, saver):
                assert (destination / MARKER).read_bytes() == (
                    workspace / MARKER
                ).read_bytes()
                with pytest.raises(ValueError, match="binding mismatch"):
                    await check_host(store, destination, fingerprint)
                with pytest.raises(ValueError, match="unfinished"):
                    await bind_host(
                        store,
                        destination,
                        fingerprint,
                        actor_id="operator",
                        note="Pending work must not be ignored",
                        replace=True,
                    )
                assert (
                    await RunRecovery(store).release(**release_args(lost))
                    == recovery_receipt
                )
                assert (
                    await RunQueue(store.engine).get(queued["id"], "actor-1") == queued
                )
                assert (await store.load_budget(context())).snapshot() == frozen_budget
                assert (await store.read_control(context())).role_data[
                    "restore_probe"
                ] == "same-control"
                assert (
                    await graph.compile(checkpointer=saver).aget_state(config)
                ).values["step"] == 1
                # Only after proving the restored snapshot is unchanged, explicitly
                # withdraw the test's queued command and adopt this restored pair.
                await RunQueue(store.engine).cancel_waiting("report-1", "actor-1")
                adopted = await bind_host(
                    store,
                    destination,
                    fingerprint,
                    actor_id="operator",
                    note="Original writers stopped; restored snapshot verified; queued command withdrawn",
                    replace=True,
                )
                assert adopted["workspace_id"] == original_binding["workspace_id"]
                assert adopted["revision"] == original_binding["revision"] + 1
                assert (
                    adopted["database_identity"]
                    != original_binding["database_identity"]
                )
                assert await check_host(store, destination, fingerprint) == adopted
                assert (await store.load_budget(context())).snapshot() == frozen_budget
            assert (destination / "report-1/report.md").read_bytes() == (
                workspace / "report-1/report.md"
            ).read_bytes()
        finally:
            for name in created:
                assert name.startswith("rc_restore_") and name != params.get("dbname")
                await admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
