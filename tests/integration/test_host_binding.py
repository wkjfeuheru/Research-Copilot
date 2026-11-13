"""Explicit host configuration binding; no model client or business DB."""

import json
import pytest
from sqlalchemy import select

from tests.integration import test_retrospective_leases as lease_fixtures
from tests.integration.test_run_queue import enqueue
from research_copilot.runtime.run_queue import RunQueue

lease_stores = lease_fixtures.lease_stores


@pytest.fixture(autouse=True)
async def no_retrospective_work(lease_stores):
    from research_copilot.runtime.experience import retrospective_jobs

    async with lease_stores[0].engine.begin() as connection:
        await connection.execute(retrospective_jobs.delete())


async def test_binding_requires_explicit_setup_and_rejects_drift(
    lease_stores, tmp_path
):
    from research_copilot.runtime.host_binding import (
        bind_host,
        check_host,
        digest,
        binding_events,
    )

    first, second = lease_stores
    root = tmp_path / "workspace"
    fingerprint = digest({"model": "offline", "extra": "private-material"})
    with pytest.raises(ValueError, match="bind-host"):
        await check_host(first, root, fingerprint)
    assert not root.exists()
    bound = await bind_host(
        first, root, fingerprint, actor_id="operator", note="Isolated adoption"
    )
    assert await check_host(second, root, fingerprint) == bound
    assert (
        await bind_host(second, root, fingerprint, actor_id="operator", note="Replay")
        == bound
    )
    with pytest.raises(ValueError, match="binding"):
        await check_host(second, tmp_path / "wrong-workspace", fingerprint)
    with pytest.raises(ValueError, match="binding"):
        await check_host(second, root, digest({"model": "changed"}))
    async with first.engine.connect() as connection:
        events = (await connection.execute(select(binding_events))).mappings().all()
    assert len(events) == 1 and events[0]["note"] == "Isolated adoption"
    assert "private-material" not in json.dumps(
        [dict(row) for row in events], default=str
    )
    assert "private-material" not in (root / ".research-copilot-host.json").read_text()


async def test_rebinding_is_explicit_audited_and_blocked_by_unfinished_commands(
    lease_stores, tmp_path
):
    from research_copilot.runtime.host_binding import (
        bind_host,
        check_host,
        digest,
        binding_events,
    )

    store = lease_stores[0]
    root = tmp_path / "workspace"
    before, after = digest({"model": "one"}), digest({"model": "two"})
    initial = await bind_host(store, root, before, actor_id="operator", note="Initial")
    with pytest.raises(ValueError, match="replace"):
        await bind_host(store, root, after, actor_id="operator", note="Change")
    queue = RunQueue(store.engine)
    await enqueue(queue)
    with pytest.raises(ValueError, match="unfinished"):
        await bind_host(
            store, root, after, actor_id="operator", note="Change", replace=True
        )
    assert await check_host(store, root, before) == initial
    await queue.cancel_waiting("report-1", "actor-1")
    changed = await bind_host(
        store,
        root,
        after,
        actor_id="operator",
        note="Reviewed configuration",
        replace=True,
    )
    assert (
        changed["revision"] == 2 and changed["workspace_id"] == initial["workspace_id"]
    )
    async with store.engine.connect() as connection:
        events = (
            (
                await connection.execute(
                    select(binding_events).order_by(binding_events.c.revision)
                )
            )
            .mappings()
            .all()
        )
    assert len(events) == 2 and events[1]["previous_digest"] == before


async def test_marker_tampering_is_not_silently_adopted(lease_stores, tmp_path):
    from research_copilot.runtime.host_binding import bind_host, check_host, digest

    store = lease_stores[0]
    root = tmp_path / "workspace"
    fingerprint = digest({"model": "offline"})
    await bind_host(store, root, fingerprint, actor_id="operator", note="Initial")
    marker = root / ".research-copilot-host.json"
    original = marker.read_bytes()
    marker.write_text('{"format": 99}', encoding="utf-8")
    with pytest.raises(ValueError, match="marker"):
        await check_host(store, root, fingerprint)
    with pytest.raises(ValueError, match="marker"):
        await bind_host(
            store,
            root,
            fingerprint,
            actor_id="operator",
            note="Must not overwrite",
            replace=True,
        )
    marker.write_bytes(original)
    assert (await check_host(store, root, fingerprint))["revision"] == 1


async def test_database_rollback_preserves_marker_and_requires_explicit_retry(
    lease_stores, tmp_path, monkeypatch
):
    from sqlalchemy import event
    from research_copilot.runtime.host_binding import (
        bind_host,
        check_host,
        digest,
        binding_events,
        MARKER,
    )

    store = lease_stores[0]
    root, fingerprint = tmp_path / "workspace", digest({"model": "offline"})

    def fail_audit(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO research_host_binding_events"):
            raise RuntimeError("Injected database failure after marker write")

    event.listen(store.engine.sync_engine, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="Injected"):
            await bind_host(store, root, fingerprint, actor_id="operator", note="First")
    finally:
        event.remove(store.engine.sync_engine, "before_cursor_execute", fail_audit)
    preserved = (root / MARKER).read_bytes()
    with pytest.raises(ValueError, match="unbound"):
        await check_host(store, root, fingerprint)
    async with store.engine.connect() as connection:
        assert not (await connection.execute(select(binding_events))).all()
    bound = await bind_host(
        store, root, fingerprint, actor_id="operator", note="Reviewed partial failure"
    )
    assert bound["revision"] == 1
    assert (root / MARKER).read_bytes() == preserved
    assert await check_host(store, root, fingerprint) == bound


async def test_another_database_cannot_adopt_marked_workspace(lease_stores, tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from research_copilot.runtime.storage import SqlSessionStore
    from research_copilot.runtime.host_binding import bind_host, digest

    root, fingerprint = tmp_path / "workspace", digest({"model": "offline"})
    await bind_host(
        lease_stores[0], root, fingerprint, actor_id="operator", note="Original"
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'foreign.db'}")
    foreign = SqlSessionStore(engine)
    try:
        await foreign.setup()
        with pytest.raises(ValueError, match="another database"):
            await bind_host(
                foreign,
                root,
                fingerprint,
                actor_id="operator",
                note="Wrong database",
                replace=True,
            )
    finally:
        await engine.dispose()


async def test_independent_stores_cannot_bind_one_database_to_two_roots(
    lease_stores, tmp_path
):
    import asyncio
    from research_copilot.runtime.host_binding import bind_host, check_host, digest

    fingerprint = digest({"model": "offline"})
    roots = [tmp_path / "one", tmp_path / "two"]
    results = await asyncio.gather(
        *(
            bind_host(
                store,
                root,
                fingerprint,
                actor_id="operator",
                note="Concurrent initial bind",
            )
            for store, root in zip(lease_stores, roots)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    for store, root, result in zip(lease_stores, roots, results):
        if isinstance(result, dict):
            assert await check_host(store, root, fingerprint) == result
        else:
            assert isinstance(result, ValueError)
            assert not root.exists()
