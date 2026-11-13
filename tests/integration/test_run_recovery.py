"""Manual release is audited and never executes or silently settles requests."""

import pytest
from sqlalchemy import select, update

from research_copilot.runtime.run_queue import QueueConflict, RunQueue, run_jobs
from research_copilot.runtime.storage import SessionBusy
from tests.integration import test_retrospective_leases as lease_fixtures
from tests.integration.test_run_queue import enqueue, expire
from tests.unit.test_orchestration import context

lease_stores = lease_fixtures.lease_stores


async def lost_command(store):
    queue = RunQueue(store.engine)
    await enqueue(queue)
    claim = await queue.claim("old-worker")
    await queue.start(claim)
    await expire(store, claim)
    return claim


def release_args(claim, **overrides):
    return {
        "report_id": "report-1",
        "actor_id": "actor-1",
        "job_id": claim["id"],
        "claim_token": claim["claim_token"],
        "owner_stopped": True,
        "note": "Isolated test owner and its execution have terminated",
    } | overrides


async def test_release_persists_audit_and_does_not_retry_or_change_budget(lease_stores):
    from research_copilot.runtime.run_recovery import RunRecovery, run_recoveries

    store, replacement = lease_stores
    claim = await lost_command(store)
    budget = await store.load_budget(context())
    request = await budget.reserve(20)
    await budget.mark_sent(request)
    await budget.settle(request, usage=None)
    before = budget.snapshot()
    recovery = RunRecovery(store)
    view = await recovery.inspect("report-1", "actor-1")
    assert view["budget"]["unknown_requests"] == 1
    assert view["budget"]["usage_verified"] is False
    result = await recovery.release(**release_args(claim))
    assert result["job_id"] == claim["id"]
    assert result["unresolved_requests"] == [request]
    assert (await store.load_budget(context())).snapshot() == before
    assert not await RunQueue(store.engine).claim("next-worker")
    assert (await RunQueue(store.engine).get(claim["id"], "actor-1"))[
        "status"
    ] == "failed"
    repeated = await RunRecovery(replacement).release(**release_args(claim))
    assert repeated == result
    async with store.engine.connect() as connection:
        assert len((await connection.execute(select(run_recoveries))).all()) == 1
        assert len((await connection.execute(select(run_jobs))).all()) == 1
    with pytest.raises(QueueConflict):
        await recovery.release(**release_args(claim, note="different evidence"))


async def test_live_owner_pending_and_stale_claim_each_block_release(lease_stores):
    from research_copilot.runtime.run_recovery import RunRecovery

    store = lease_stores[0]
    queue = RunQueue(store.engine)
    await enqueue(queue)
    claim = await queue.claim("live-worker")
    await queue.start(claim)
    recovery = RunRecovery(store)
    with pytest.raises(QueueConflict):
        await recovery.release(**release_args(claim))
    await expire(store, claim)
    async with store.lock("report-1"):
        with pytest.raises(SessionBusy):
            await recovery.release(**release_args(claim))
    with pytest.raises(QueueConflict):
        await recovery.release(**release_args(claim, claim_token="outdated"))
    budget = await store.load_budget(context())
    request = await budget.reserve(20)
    await budget.mark_sent(request)
    with pytest.raises(QueueConflict, match="pending"):
        await recovery.release(**release_args(claim))
    assert request in (await store.load_budget(context())).snapshot()["pending"]
    assert (await queue.get(claim["id"], "actor-1"))["status"] == "running"


async def test_release_restores_capacity_but_old_owner_cannot_publish(lease_stores):
    from research_copilot.runtime.run_recovery import RunRecovery
    from research_copilot.runtime.run_queue import StaleRunClaim, run_queue_settings
    from tests.integration.test_run_queue import add_report

    store = lease_stores[0]
    async with store.engine.begin() as connection:
        await connection.execute(update(run_queue_settings).values(max_active=1))
    claim = await lost_command(store)
    queue = RunQueue(store.engine)
    await add_report(store, "waiting-report")
    waiting = await enqueue(queue, "waiting-report")
    assert await queue.claim("waiting-worker") is None
    await RunRecovery(store).release(**release_args(claim))
    for operation in (queue.start, queue.renew, queue.defer):
        with pytest.raises(StaleRunClaim):
            await operation(claim)
    with pytest.raises(StaleRunClaim):
        await queue.finish(claim, succeeded=True)
    assert (await queue.claim("waiting-worker"))["id"] == waiting["id"]


async def test_release_requires_confirmation_and_matching_actor(lease_stores):
    from research_copilot.runtime.run_recovery import RunRecovery

    claim = await lost_command(lease_stores[0])
    recovery = RunRecovery(lease_stores[0])
    for overrides in (
        {"owner_stopped": False},
        {"owner_stopped": "true"},
        {"note": " "},
    ):
        with pytest.raises(ValueError):
            await recovery.release(**release_args(claim, **overrides))
    with pytest.raises(LookupError):
        await recovery.release(**release_args(claim, actor_id="another-actor"))


async def test_audit_failure_rolls_back_release(lease_stores, monkeypatch):
    from research_copilot.runtime.run_recovery import RunRecovery
    from sqlalchemy.ext.asyncio import AsyncConnection

    store = lease_stores[0]
    claim = await lost_command(store)
    execute = AsyncConnection.execute

    async def fail_audit(self, statement, *args, **kwargs):
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "research_run_recoveries"
        ):
            raise RuntimeError("injected audit failure")
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", fail_audit)
    with pytest.raises(RuntimeError, match="injected"):
        await RunRecovery(store).release(**release_args(claim))
    assert (await RunQueue(store.engine).get(claim["id"], "actor-1"))[
        "status"
    ] == "running"


async def test_report_identity_drift_blocks_release(lease_stores):
    from research_copilot.runtime.run_recovery import RunRecovery
    from research_copilot.runtime.storage import sessions

    store = lease_stores[0]
    claim = await lost_command(store)
    async with store.engine.begin() as connection:
        await connection.execute(update(sessions).values(run_id="changed-run"))
    with pytest.raises(QueueConflict):
        await RunRecovery(store).release(**release_args(claim))


async def test_second_store_cannot_release_while_postgres_owner_holds_lock(
    lease_stores,
):
    from research_copilot.runtime.run_recovery import RunRecovery

    first, second = lease_stores
    if first.engine.dialect.name != "postgresql":
        pytest.skip("Cross-process report ownership requires PostgreSQL")
    claim = await lost_command(first)
    async with first.lock("report-1"):
        with pytest.raises(SessionBusy):
            await RunRecovery(second).release(**release_args(claim))
    assert (await RunRecovery(second).release(**release_args(claim)))[
        "job_id"
    ] == claim["id"]


async def test_web_release_requires_explicit_evidence_and_never_starts_worker(
    lease_stores, tmp_path
):
    from research_copilot.web.app import WorkbenchBackend
    from tests.integration.test_durable_web_runs import client
    from tests.integration.test_web_app import FakeSession, POLICY

    store = lease_stores[0]
    claim = await lost_command(store)
    current = WorkbenchBackend(
        session=FakeSession(store),
        store=store,
        actor_id="actor-1",
        workspace_root=tmp_path,
        execution_policy=POLICY,
    )
    async with client(current) as http:
        state = await http.get("/api/reports/report-1/run-recovery")
        assert state.status_code == 200
        assert state.json()["command"]["claim_token"] == claim["claim_token"]
        assert state.json()["can_release"] is True
        url = f"/api/reports/report-1/runs/{claim['id']}/release"
        body = {
            key: release_args(claim)[key]
            for key in ("claim_token", "owner_stopped", "note")
        }
        assert (
            await http.post(url, json=body | {"owner_stopped": "true"})
        ).status_code == 422
        assert (
            await http.post(url, json=body | {"authorize_retry": True})
        ).status_code == 422
        assert (
            await http.post(url, json=body | {"owner_stopped": False})
        ).status_code == 400
        released = await http.post(url, json=body)
        assert released.status_code == 200
        assert (await http.post(url, json=body)).json() == released.json()
        assert current.runs.worker._poller is None
        assert not current.session.entered.is_set()
        state = (await http.get("/api/reports/report-1/run-recovery")).json()
        assert not state["can_release"]
        assert state["receipt"] == released.json()
        assert (await http.get("/api/reports/missing/run-recovery")).status_code == 404
        await current.runs.aclose()
        assert (await http.post(url, json=body)).status_code == 503


async def test_web_request_recovery_separates_unknown_usage_and_retry(
    lease_stores, tmp_path
):
    from research_copilot.web.app import WorkbenchBackend
    from tests.integration.test_durable_web_runs import client
    from tests.integration.test_web_app import FakeSession, POLICY

    store = lease_stores[0]
    await lost_command(store)
    budget = await store.load_budget(context())
    request = await budget.reserve(20, operation_id="frozen-node")
    await budget.mark_sent(request)
    current = WorkbenchBackend(
        session=FakeSession(store),
        store=store,
        actor_id="actor-1",
        workspace_root=tmp_path,
        execution_policy=POLICY,
    )
    async with client(current) as http:
        state = (await http.get("/api/reports/report-1/run-recovery")).json()
        url = f"/api/reports/report-1/requests/{request}/recovery"
        body = dict(
            action="resolve_pending",
            expected_revision=state["budget_revision"],
            owner_stopped=True,
            note="Offline owner stopped; provider result unavailable",
        )
        assert (
            await http.post(url, json=body | {"owner_stopped": False})
        ).status_code == 400
        assert (await http.post(url, json=body)).status_code == 200
        assert (await http.post(url, json=body)).status_code == 409
        state = (await http.get("/api/reports/report-1/run-recovery")).json()
        assert state["budget"]["unknown_requests"] == 1
        assert state["requests"][0]["retry_authorization"] is None
        body = body | {
            "action": "reconcile",
            "expected_revision": state["budget_revision"],
            "usage": 8,
        }
        assert (await http.post(url, json=body)).status_code == 200
        state = (await http.get("/api/reports/report-1/run-recovery")).json()
        assert state["budget"]["charged_tokens"] == 8
        body = dict(
            action="authorize_retry",
            expected_revision=state["budget_revision"],
            owner_stopped=True,
            note="Allow one test retry",
        )
        assert (await http.post(url, json=body)).status_code == 400
        assert (
            await http.post(url, json=body | {"acknowledge_duplicate_charge": True})
        ).status_code == 200
        state = (await http.get("/api/reports/report-1/run-recovery")).json()
        assert state["requests"][0]["retry_authorization"]["note"] == body["note"]
        assert state["budget"]["calls"] == 1
        assert (
            current.runs.worker._poller is None and not current.session.entered.is_set()
        )


async def test_web_over_budget_recovery_reports_saved_state(lease_stores, tmp_path):
    from research_copilot.web.app import WorkbenchBackend
    from tests.integration.test_durable_web_runs import client
    from tests.integration.test_web_app import FakeSession, POLICY

    store = lease_stores[0]
    budget = await store.load_budget(context())
    request_id = await budget.reserve(20)
    await budget.mark_sent(request_id)
    current = WorkbenchBackend(
        session=FakeSession(store),
        store=store,
        actor_id="actor-1",
        workspace_root=tmp_path,
        execution_policy=POLICY,
    )
    async with client(current) as http:
        result = await http.post(
            f"/api/reports/report-1/requests/{request_id}/recovery",
            json={
                "action": "resolve_pending",
                "expected_revision": budget.revision,
                "owner_stopped": True,
                "note": "Verified usage exceeded cap",
                "usage": 100001,
            },
        )
        assert result.status_code == 409
        assert "已保存" in result.json()["detail"]
        state = (await http.get("/api/reports/report-1/run-recovery")).json()
        assert (
            state["budget"]["charged_tokens"] == 100001
            and not state["pending_requests"]
        )
