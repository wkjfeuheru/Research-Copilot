"""Web admission must persist commands atomically and project across backends."""

import httpx
import asyncio
from sqlalchemy import select, update

from research_copilot.runtime.run_queue import run_jobs, run_queue_settings
from research_copilot.web.app import WorkbenchBackend, create_app
from tests.integration import test_retrospective_leases as lease_fixtures
from tests.integration.test_web_app import FakeSession, POLICY

lease_stores = lease_fixtures.lease_stores


async def no_start():
    pass


def backend(store, root):
    session = FakeSession(store)
    session.gate.set()
    return WorkbenchBackend(
        session=session,
        store=store,
        actor_id="local",
        workspace_root=root,
        execution_policy=POLICY,
    )


def client(current):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend=current)),
        base_url="http://localhost",
    )


async def test_created_command_survives_backend_replacement_and_blocks_delete(
    lease_stores, tmp_path, monkeypatch
):
    first = backend(lease_stores[0], tmp_path)
    second = backend(lease_stores[1], tmp_path)
    monkeypatch.setattr(first.runs.worker, "start", no_start)
    async with client(first) as http:
        created = await http.post(
            "/api/reports",
            headers={"Idempotency-Key": "create-1"},
            json={"user_request": "queued research"},
        )
        assert created.status_code == 201
        rid = created.json()["report"]["id"]
        duplicate = await http.post(
            "/api/reports",
            headers={"Idempotency-Key": "create-1"},
            json={"user_request": "queued research"},
        )
        assert duplicate.json()["report"]["id"] == rid
    async with client(second) as http:
        workspace = (await http.get(f"/api/reports/{rid}")).json()
        assert workspace["runtime"]["busy"] is True
        assert workspace["runtime"]["queue_state"] == "pending"
        assert (await http.delete(f"/api/reports/{rid}")).status_code == 409
        assert not second.session.entered.is_set()
        assert await second.runs.worker.run_once()
        workspace = (await http.get(f"/api/reports/{rid}")).json()
        assert workspace["runtime"]["busy"] is False
        assert workspace["runtime"]["queue_state"] == "succeeded"
        assert second.session.entered.is_set()
    async with lease_stores[0].engine.connect() as connection:
        jobs = (await connection.execute(select(run_jobs))).mappings().all()
    assert len(jobs) == 1 and jobs[0]["kind"] == "start"


async def test_queue_full_rolls_back_new_session_and_cancel_is_durable(
    lease_stores, tmp_path, monkeypatch
):
    current = backend(lease_stores[0], tmp_path)
    monkeypatch.setattr(current.runs.worker, "start", no_start)
    async with lease_stores[0].engine.begin() as connection:
        await connection.execute(update(run_queue_settings).values(max_pending=1))
    async with client(current) as http:
        first = await http.post("/api/reports", json={"user_request": "first"})
        assert first.status_code == 201
        rid = first.json()["report"]["id"]
        rejected = await http.post("/api/reports", json={"user_request": "full"})
        assert rejected.status_code == 429
        assert len(await current.store.list_for_actor("local")) == 1
        assert (await http.post(f"/api/reports/{rid}/cancel")).status_code == 200
    replacement = backend(lease_stores[1], tmp_path)
    async with client(replacement) as http:
        workspace = (await http.get(f"/api/reports/{rid}")).json()
        assert not workspace["runtime"]["busy"]
        assert workspace["runtime"]["queue_state"] == "cancelled"
        assert not await replacement.runs.worker.run_once()
        assert not replacement.session.entered.is_set()


async def test_shutdown_refuses_web_admission_before_database_write(
    lease_stores, tmp_path
):
    current = backend(lease_stores[0], tmp_path)
    await current.runs.aclose()
    async with client(current) as http:
        assert (
            await http.post("/api/reports", json={"user_request": "closed"})
        ).status_code == 503
    assert await current.store.list_for_actor("local") == []


async def test_expired_execution_projects_recovery_without_resume_or_delete(
    lease_stores, tmp_path, monkeypatch
):
    from dataclasses import replace

    from tests.integration.test_run_queue import expire

    current = backend(lease_stores[0], tmp_path)
    monkeypatch.setattr(current.runs.worker, "start", no_start)
    async with client(current) as http:
        response = await http.post("/api/reports", json={"user_request": "lost owner"})
        rid = response.json()["report"]["id"]
        claim = await current.runs.queue.claim("lost-worker")
        await current.runs.queue.start(claim)
        record = await current.store.load(rid, "local")
        await current.store.save(replace(record, status="running", run_id="run-1"))
        await expire(current.store, claim)
        assert await current.runs.queue.claim("replacement") is None
        state = (await http.get(f"/api/reports/{rid}")).json()
        assert state["runtime"]["queue_state"] == "recovery_required"
        assert not state["runtime"]["busy"] and not state["runtime"]["recoverable"]
        assert not {"resume", "correct"}.intersection(
            a["key"] for a in state["actions"]
        )
        assert (await http.delete(f"/api/reports/{rid}")).status_code == 409
        assert (await http.post(f"/api/reports/{rid}/resume")).status_code == 409
        assert (await http.get("/api/reports")).json()["active_report_id"] is None
        assert not current.session.entered.is_set()


async def test_two_web_backends_share_running_limit(lease_stores, tmp_path):
    first, second = [backend(store, tmp_path) for store in lease_stores]
    first.session.gate.clear()
    second.session.gate.clear()
    async with lease_stores[0].engine.begin() as connection:
        await connection.execute(
            update(run_queue_settings).values(max_active=1, max_pending=2)
        )
    try:
        async with client(first) as http1, client(second) as http2:
            assert (
                await http1.post("/api/reports", json={"user_request": "one"})
            ).status_code == 201
            await asyncio.wait_for(first.session.entered.wait(), 3)
            for text in ("two", "three"):
                assert (
                    await http2.post("/api/reports", json={"user_request": text})
                ).status_code == 201
            await asyncio.sleep(0.3)
            async with lease_stores[1].engine.connect() as connection:
                statuses = (
                    (await connection.execute(select(run_jobs.c.status)))
                    .scalars()
                    .all()
                )
            assert statuses.count("running") == 1 and statuses.count("pending") == 2
            assert not second.session.entered.is_set()
            assert (
                await http1.post("/api/reports", json={"user_request": "full"})
            ).status_code == 429
    finally:
        first.session.gate.set()
        second.session.gate.set()
        await first.runs.drain()
        await second.runs.drain()
        await first.runs.aclose()
        await second.runs.aclose()


async def test_shutdown_orders_after_inflight_admission(
    lease_stores, tmp_path, monkeypatch
):
    current = backend(lease_stores[0], tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = current.store.create_in

    async def slow_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(current.store, "create_in", slow_create)
    async with client(current) as http:
        request = asyncio.create_task(
            http.post("/api/reports", json={"user_request": "in flight"})
        )
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(current.runs.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        response = await request
        assert response.status_code == 201
        await closing
        assert (
            await http.post("/api/reports", json={"user_request": "too late"})
        ).status_code == 503
    assert len(await current.store.list_for_actor("local")) == 1
