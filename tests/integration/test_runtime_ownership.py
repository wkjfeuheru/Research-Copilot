"""Durable unknown runtime ownership; only isolated PostgreSQL, no providers."""

import asyncio

import psycopg
import pytest

from research_copilot.runtime.storage import postgres_runtime
from tests.integration.test_maintenance_postgres import databases

pytestmark = pytest.mark.db
__all__ = ["databases"]


async def test_connected_owner_is_visible_and_cannot_be_operator_released(databases):
    from research_copilot.runtime.runtime_ownership import list_owners, recover_owner

    url = databases[0]
    async with postgres_runtime(url):
        rows = await list_owners(url)
        assert len(rows) == 1 and rows[0]["connected"] is True
        with pytest.raises(RuntimeError, match="maintenance"):
            await recover_owner(
                url,
                rows[0]["id"],
                actor_id="operator",
                note="Still connected",
                owner_stopped=True,
            )
        async with postgres_runtime(url):
            assert len(await list_owners(url)) == 2
    assert await list_owners(url) == []


async def test_lost_owner_blocks_maintenance_and_new_writers_until_audited_recovery(
    databases, tmp_path
):
    from research_copilot.runtime.runtime_ownership import list_owners, recover_owner
    from research_copilot.runtime.backup import backup

    url = databases[0]
    with pytest.raises(RuntimeError, match="ownership"):
        async with postgres_runtime(url):
            owner = (await list_owners(url))[0]
            async with await psycopg.AsyncConnection.connect(
                url, autocommit=True
            ) as admin:
                await admin.execute(
                    "SELECT pg_terminate_backend(%s, 5000)", (owner["backend_pid"],)
                )
    rows = await list_owners(url)
    assert len(rows) == 1 and rows[0]["connected"] is False
    for kwargs in [{}, {"initialize": True}, {"maintenance": True}]:
        with pytest.raises(RuntimeError, match="Unsettled runtime"):
            async with postgres_runtime(url, **kwargs):
                pytest.fail("Unknown old owner was ignored")
    async with postgres_runtime(url, read_only=True) as (store, _):
        await store.probe()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="Unsettled runtime"):
        await asyncio.to_thread(backup, url, workspace, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    with pytest.raises(ValueError, match="stopped"):
        await recover_owner(
            url,
            owner["id"],
            actor_id="operator",
            note="No confirmation",
            owner_stopped=False,
        )
    evidence = dict(
        actor_id="operator",
        note="Test body ended; no external execution was started",
        owner_stopped=True,
    )
    receipt = await recover_owner(url, owner["id"], **evidence)
    assert receipt["state"] == "recovered" and receipt["recovery_actor"] == "operator"
    assert await recover_owner(url, owner["id"], **evidence) == receipt
    with pytest.raises(ValueError, match="different"):
        await recover_owner(
            url, owner["id"], **{**evidence, "note": "Changed evidence"}
        )
    assert await list_owners(url) == []
    async with postgres_runtime(url, initialize=True):
        pass


async def test_read_only_runtime_does_not_write_owner_history(databases):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    url = databases[0]
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as inspect:
        before = await (
            await inspect.execute("SELECT * FROM runtime_owners ORDER BY id")
        ).fetchall()
        async with postgres_runtime(url, read_only=True) as (store, _):
            with pytest.raises(DBAPIError, match="read-only"):
                async with store.engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE runtime_owners SET state=state")
                    )
        after = await (
            await inspect.execute("SELECT * FROM runtime_owners ORDER BY id")
        ).fetchall()
        assert before == after
