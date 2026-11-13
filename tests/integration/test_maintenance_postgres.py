"""Database-wide maintenance exclusion on disposable databases; no models."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import URL

from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.urls import to_checkpoint_url

pytestmark = pytest.mark.db
LOCK_KEY = 725231110


@pytest.fixture
async def databases():
    base = os.environ.get("RC_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Explicit isolated RC_TEST_DATABASE_URL is required")
    base = to_checkpoint_url(base)
    params = psycopg.conninfo.conninfo_to_dict(base)
    names, urls = [], []
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        try:
            for _ in range(2):
                name = "rc_maintenance_" + uuid4().hex
                await admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                )
                names.append(name)
                urls.append(
                    URL.create(
                        "postgresql",
                        username=params.get("user"),
                        password=params.get("password"),
                        host=params.get("host"),
                        port=int(params.get("port", 5432)),
                        database=name,
                    ).render_as_string(hide_password=False)
                )
            async with postgres_runtime(urls[0], initialize=True):
                pass
            yield urls
        finally:
            for name in names:
                assert name.startswith("rc_maintenance_") and name != params.get(
                    "dbname"
                )
                await admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )


async def test_two_runtime_readers_coexist_but_block_schema_maintenance(databases):
    url = databases[0]
    async with postgres_runtime(url):
        async with postgres_runtime(url):
            with pytest.raises(RuntimeError, match="maintenance"):
                async with postgres_runtime(url, initialize=True):
                    pass
    async with postgres_runtime(url, initialize=True):
        pass


@pytest.mark.parametrize("initialize", [False, True])
async def test_maintenance_blocks_startup_before_opening_dependencies(
    databases, initialize
):
    url = databases[0]
    async with await psycopg.AsyncConnection.connect(
        url, autocommit=True
    ) as connection:
        await connection.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        with pytest.raises(RuntimeError, match="maintenance"):
            async with postgres_runtime(url, initialize=initialize):
                pass


async def test_backup_cannot_bypass_runtime_using_another_workspace(
    databases, tmp_path
):
    from research_copilot.runtime.backup import backup

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = tmp_path / "bundle"
    async with postgres_runtime(databases[0]):
        with pytest.raises(RuntimeError, match="maintenance"):
            await asyncio.to_thread(backup, databases[0], workspace, destination)
    assert not destination.exists()


async def test_restore_retains_exclusion_through_external_tool_and_verification(
    databases, tmp_path, monkeypatch
):
    from research_copilot.runtime import backup as module

    source, target = databases
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "evidence.txt").write_text("Frozen bytes", encoding="utf-8")
    bundle, destination = tmp_path / "bundle", tmp_path / "restored"
    await asyncio.to_thread(module.backup, source, workspace, bundle)
    real_tool, real_inventory = module._pg_tool, module.database_inventory
    probes = []

    def assert_excluded(stage):
        with psycopg.connect(target, autocommit=True) as probe:
            acquired = probe.execute(
                "SELECT pg_try_advisory_lock_shared(%s)", (LOCK_KEY,)
            ).fetchone()[0]
            assert acquired is False, f"Runtime can enter during restore {stage}"
            probes.append(stage)

    def tool(arguments, database_url):
        if arguments[0] == "pg_restore":
            assert_excluded("external tool")
        return real_tool(arguments, database_url)

    def inventory(connection):
        assert_excluded("verification")
        return real_inventory(connection)

    monkeypatch.setattr(module, "_pg_tool", tool)
    monkeypatch.setattr(module, "database_inventory", inventory)
    await asyncio.to_thread(module.restore, target, bundle, destination)
    assert probes == ["external tool", "verification"]
    assert (destination / "evidence.txt").read_bytes() == (
        workspace / "evidence.txt"
    ).read_bytes()
    async with postgres_runtime(target):
        pass


async def test_binding_configuration_requires_database_maintenance(databases, tmp_path):
    from research_copilot.host import bind_host_configuration
    from research_copilot.runtime.host_settings import HostSettings

    settings = HostSettings(_env_file=None, database_url=databases[0])
    async with postgres_runtime(databases[0]):
        with pytest.raises(RuntimeError, match="maintenance"):
            await bind_host_configuration(
                settings,
                models_path=Path("config/models.example.toml"),
                workspace_root=tmp_path / "workspace",
                policy_version="test-policy",
                mode="test",
                actor_id="operator",
                note="Host still open",
            )
    assert not (tmp_path / "workspace").exists()


async def test_guard_connection_loss_is_reported_and_never_silently_reacquired(
    databases,
):
    from research_copilot.runtime.maintenance import async_database_access

    url = databases[0]
    with pytest.raises(RuntimeError, match="maintenance ownership lost"):
        async with async_database_access(url) as guarded:
            pid = (await (await guarded.execute("SELECT pg_backend_pid()")).fetchone())[
                0
            ]
            async with await psycopg.AsyncConnection.connect(
                url, autocommit=True
            ) as admin:
                await admin.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,))
    # No reconnect/reacquire attempt was hidden inside the guard.
    async with postgres_runtime(url, initialize=True):
        pass


async def test_cancelled_runtime_keeps_lock_until_dependencies_finish(
    databases, monkeypatch
):
    from contextlib import asynccontextmanager
    from research_copilot.runtime import storage

    entered, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def slow_dependencies(*args, **kwargs):
        try:
            yield None, None
        finally:
            closing.set()
            await finish.wait()

    monkeypatch.setattr(storage, "_postgres_runtime", slow_dependencies)

    async def run():
        async with storage.postgres_runtime(databases[0]):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        assert not task.done()
        with pytest.raises(RuntimeError, match="maintenance"):
            async with storage.postgres_runtime(databases[0], initialize=True):
                pass
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with storage.postgres_runtime(databases[0], initialize=True):
        pass


@pytest.mark.parametrize("exit_kind", ["normal", "kill"])
async def test_independent_process_owner_excludes_maintenance_until_exit(
    databases, tmp_path, exit_kind
):
    import subprocess
    import sys

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("RC_")
    }
    environment["RC_TEST_DATABASE_URL"] = databases[0]
    process = None
    with (tmp_path / "owner.log").open("wb") as log:
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.fakes.maintenance_owner", str(tmp_path)],
                cwd=Path(__file__).resolve().parents[2],
                env=environment,
                stdout=log,
                stderr=log,
            )
            async with asyncio.timeout(45):
                while not (tmp_path / "ready").exists():
                    assert process.poll() is None, (tmp_path / "owner.log").read_text(
                        "utf-8", errors="replace"
                    )
                    await asyncio.sleep(0.05)
            with pytest.raises(RuntimeError, match="maintenance"):
                async with postgres_runtime(databases[0], initialize=True):
                    pass
            if exit_kind == "normal":
                (tmp_path / "stop").write_text("stop", encoding="utf-8")
            else:
                process.kill()
            async with asyncio.timeout(20):
                while process.poll() is None:
                    await asyncio.sleep(0.05)
            assert (tmp_path / "closed").exists() is (exit_kind == "normal")
            assert (process.returncode == 0) is (exit_kind == "normal")
            if exit_kind == "kill":
                from research_copilot.runtime.runtime_ownership import (
                    list_owners,
                    recover_owner,
                )

                with pytest.raises(RuntimeError, match="Unsettled runtime"):
                    async with postgres_runtime(databases[0], initialize=True):
                        pass
                owner = (await list_owners(databases[0]))[0]
                assert owner["connected"] is False
                await recover_owner(
                    databases[0],
                    owner["id"],
                    actor_id="test-operator",
                    note="Tracked child has exited; this fixture starts no external work",
                    owner_stopped=True,
                )
            async with postgres_runtime(databases[0], initialize=True):
                pass
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)
