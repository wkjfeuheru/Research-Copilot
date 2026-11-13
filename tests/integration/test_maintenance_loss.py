"""Maintenance death must fence new work, including before business DDL exists."""

import asyncio

import psycopg
import pytest

from research_copilot.runtime.maintenance import (
    MaintenanceBusy,
    MaintenanceLost,
    async_database_access,
    maintenance_connection,
)
from research_copilot.runtime.storage import postgres_runtime
from tests.integration.test_maintenance_postgres import databases

pytestmark = pytest.mark.db
__all__ = ["databases"]


@pytest.mark.parametrize("empty", [False, True])
async def test_disconnected_sync_maintenance_blocks_new_work(databases, empty):
    url = databases[int(empty)]
    scope = maintenance_connection(url)
    owner = await asyncio.to_thread(scope.__enter__)
    try:
        pid = await asyncio.to_thread(owner.execute, "SELECT pg_backend_pid()")
        pid = (await asyncio.to_thread(pid.fetchone))[0]
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
            await admin.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,))
        # The old operation is deliberately still inside its scope.
        with pytest.raises(MaintenanceBusy, match="Unsettled maintenance"):
            async with async_database_access(url):
                pytest.fail("Maintenance disconnected but old operation is still live")
        with pytest.raises(MaintenanceBusy, match="Unsettled maintenance"):
            async with postgres_runtime(url, initialize=True):
                pytest.fail("Another migration entered an unsettled maintenance scope")
    finally:
        with pytest.raises(MaintenanceLost):
            await asyncio.to_thread(scope.__exit__, None, None, None)


async def test_initialization_loss_before_business_schema_blocks_restart(
    databases, monkeypatch
):
    from contextlib import asynccontextmanager
    from research_copilot.runtime import storage

    url = databases[1]

    @asynccontextmanager
    async def interrupted_setup(*args, owner_connection, **kwargs):
        pid = (
            await (await owner_connection.execute("SELECT pg_backend_pid()")).fetchone()
        )[0]
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
            await admin.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,))
        with pytest.raises(MaintenanceBusy, match="Unsettled maintenance"):
            async with async_database_access(url):
                pytest.fail("Unfinished initialization was forgotten")
        yield None, None

    monkeypatch.setattr(storage, "_postgres_runtime", interrupted_setup)
    with pytest.raises(MaintenanceLost):
        async with postgres_runtime(url, initialize=True):
            pass


async def test_restore_loss_preserves_target_journal_until_explicit_recovery(
    databases, tmp_path, monkeypatch
):
    import threading
    from research_copilot.runtime import backup as module
    from research_copilot.runtime.maintenance_ownership import (
        list_owners,
        recover_owner,
    )

    source, target = databases
    workspace, restored, bundle = (
        tmp_path / name for name in ("source", "restored", "bundle")
    )
    workspace.mkdir()
    (workspace / "evidence.txt").write_text("Frozen evidence", encoding="utf-8")
    await asyncio.to_thread(module.backup, source, workspace, bundle)
    entered, finish = threading.Event(), threading.Event()
    real_tool = module._pg_tool

    def delayed_tool(arguments, database_url):
        real_tool(arguments, database_url)
        if arguments[0] == "pg_restore":
            entered.set()
            assert finish.wait(30), "Test did not release completed restore tool"

    monkeypatch.setattr(module, "_pg_tool", delayed_tool)
    task = asyncio.create_task(
        asyncio.to_thread(module.restore, target, bundle, restored)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        owner = (await list_owners(target))[0]
        assert owner["operation"] == "restore" and owner["connected"] is True
        with pytest.raises(MaintenanceBusy):
            await recover_owner(
                target,
                owner["id"],
                actor_id="operator",
                note="Still live",
                owner_stopped=True,
            )
        async with await psycopg.AsyncConnection.connect(
            target, autocommit=True
        ) as admin:
            await admin.execute(
                "SELECT pg_terminate_backend(%s, 5000)", (owner["backend_pid"],)
            )
            # This journal belongs to the target, not the source dump.
            rows = await (
                await admin.execute("SELECT id FROM rc_maintenance.operations")
            ).fetchall()
            assert rows == [(owner["id"],)]
        with pytest.raises(MaintenanceBusy, match="Unsettled maintenance"):
            async with postgres_runtime(target):
                pytest.fail("Runtime entered while restore had not returned")
    finally:
        finish.set()
        with pytest.raises(MaintenanceLost):
            await task
    assert (await list_owners(target))[0]["connected"] is False
    evidence = dict(
        actor_id="operator",
        note="Tracked restore and external tool returned; no work remains",
        owner_stopped=True,
    )
    with pytest.raises(ValueError, match="stopped"):
        await recover_owner(target, owner["id"], **{**evidence, "owner_stopped": False})
    receipt = await recover_owner(target, owner["id"], **evidence)
    assert receipt["state"] == "recovered"
    assert await recover_owner(target, owner["id"], **evidence) == receipt
    with pytest.raises(ValueError, match="different"):
        await recover_owner(
            target, owner["id"], **{**evidence, "note": "Changed evidence"}
        )
    assert await list_owners(target) == []
    async with postgres_runtime(target):
        pass
    assert (restored / "evidence.txt").read_bytes() == (
        workspace / "evidence.txt"
    ).read_bytes()


async def test_foreign_control_schema_is_rejected_without_adoption(databases):
    url = databases[1]
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute("CREATE SCHEMA rc_maintenance")
        await admin.execute("CREATE TABLE rc_maintenance.foreign_data (value text)")
        await admin.execute(
            "INSERT INTO rc_maintenance.foreign_data VALUES ('retained')"
        )
        with pytest.raises(ValueError, match="control schema"):
            async with postgres_runtime(url, initialize=True):
                pass
        assert await (
            await admin.execute("SELECT * FROM rc_maintenance.foreign_data")
        ).fetchall() == [("retained",)]
        assert (
            await (
                await admin.execute("SELECT to_regclass('public.sessions')")
            ).fetchone()
        )[0] is None


async def test_killed_maintenance_process_leaves_durable_barrier(databases, tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from research_copilot.runtime.maintenance_ownership import (
        list_owners,
        recover_owner,
    )

    url = databases[1]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("RC_")
    }
    environment["RC_TEST_DATABASE_URL"] = url
    # Windows venv's python.exe can be a launcher with a different child PID.
    # Launch the interpreter itself so process.kill()/wait() cover the actual owner.
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    with (tmp_path / "child.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys._base_executable,
                "-m",
                "tests.fakes.maintenance_owner",
                str(tmp_path),
                "maintenance",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with asyncio.timeout(30):
                while not (tmp_path / "ready").exists():
                    assert process.poll() is None, (tmp_path / "child.log").read_text(
                        "utf-8", errors="replace"
                    )
                    await asyncio.sleep(0.05)
            owner = (await list_owners(url))[0]
            assert owner["process_id"] == process.pid and owner["connected"]
            process.kill()
            await asyncio.to_thread(process.wait, timeout=10)
            assert not (tmp_path / "closed").exists()
            with pytest.raises(MaintenanceBusy, match="Unsettled maintenance"):
                async with postgres_runtime(url, initialize=True):
                    pass
            assert (await list_owners(url))[0]["connected"] is False
            await recover_owner(
                url,
                owner["id"],
                actor_id="operator",
                note="Tracked child exited; fixture starts no external execution",
                owner_stopped=True,
            )
            async with postgres_runtime(url, initialize=True):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)


async def test_reserved_schema_rejects_unrelated_collation(databases):
    url = databases[0]
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(
            'CREATE COLLATION rc_maintenance.foreign_collation FROM "C"'
        )
        with pytest.raises(ValueError, match="control schema"):
            async with async_database_access(url):
                pytest.fail("Reserved schema contains unrelated objects")
