"""Operator recovery over the actual HTTP route and isolated durable stores."""

from uuid import uuid4
import asyncio

import httpx
import pytest

from research_copilot.runtime.experience import ExperienceService
from research_copilot.runtime.retrospective_usage import record_attempt
from research_copilot.web.app import WorkbenchBackend, create_app
from tests.integration.test_experience_api import IdleSession
from tests.integration import test_retrospective_leases as lease_fixtures
from tests.unit.test_orchestration import context

lease_stores = lease_fixtures.lease_stores


@pytest.fixture
async def recovery_client(lease_stores, tmp_path):
    store, _ = lease_stores
    claim = await store.experience.claim()
    await record_attempt(store.engine, claim, status="started", usage={})
    await store.experience.release(claim, error="isolated lost response")
    backend = WorkbenchBackend(
        session=IdleSession(),
        store=store,
        actor_id="actor-1",
        workspace_root=tmp_path / "workspace",
        experience=ExperienceService(store),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend=backend)),
        base_url="http://localhost",
    ) as client:
        yield client, store, claim


def correction(claim, **changes):
    return {
        "action": "reconcile",
        "operation_id": uuid4().hex,
        "expected_revision": 0,
        "attempt_id": claim["claim_token"],
        "input_tokens": 5,
        "output_tokens": 3,
        "note": "Verified against isolated provider response fixture",
        **changes,
    }


def retry(**changes):
    return {
        "action": "authorize_retry",
        "operation_id": uuid4().hex,
        "expected_revision": 0,
        "acknowledge_duplicate_charge": True,
        "note": "Authorize one additional fixture attempt",
        **changes,
    }


def endpoint(claim):
    return f"/api/reports/report-1/retrospectives/{claim['id']}/recovery"


async def test_reconcile_is_audited_idempotent_and_does_not_retry(recovery_client):
    client, store, claim = recovery_client
    body = correction(claim)
    response = await client.post(endpoint(claim), json=body)
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["status"] == "failed" and job["recovery_revision"] == 1
    assert job["usage"]["total_tokens"] == 8 and job["usage"]["unknown_attempts"] == 0
    audit = job["usage"]["recovery_audit"]
    assert len(audit) == 1 and audit[0]["actor_id"] == "actor-1"
    assert audit[0]["previous_usage"] == {} and audit[0]["note"] == body["note"]
    replay = await client.post(endpoint(claim), json=body)
    assert replay.status_code == 200 and replay.json()["job"] == job
    assert await store.experience.claim() is None
    assert (await store.load_budget(context())).calls == 0
    conflict = await client.post(endpoint(claim), json={**body, "input_tokens": 6})
    assert conflict.status_code == 409
    stale = await client.post(endpoint(claim), json=correction(claim))
    assert stale.status_code == 409


async def test_explicit_retry_is_consumed_once_and_preserves_unknown_ledger(
    recovery_client,
):
    client, store, claim = recovery_client
    body = retry()
    response = await client.post(endpoint(claim), json=body)
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["status"] == "pending" and job["max_attempts"] == 2
    assert job["usage"]["unknown_attempts"] == 1
    retried = await store.experience.claim()
    assert retried and retried["attempts"] == 2
    assert (await client.post(endpoint(claim), json=body)).status_code == 200
    assert await store.experience.claim() is None
    await record_attempt(store.engine, retried, status="started", usage={})
    await record_attempt(
        store.engine,
        retried,
        status="completed",
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )
    await store.experience.complete(retried, [])
    done = (await store.experience.jobs("report-1"))[0]
    assert done["usage"]["unknown_attempts"] == 1
    assert len(done["usage"]["recovery_audit"]) == 1
    # Historical unknown usage may still be reconciled after the retry completes.
    fixed = await client.post(
        endpoint(claim), json=correction(claim, expected_revision=1)
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["job"]["usage"]["total_tokens"] == 20
    assert fixed.json()["job"]["status"] == "completed"
    assert (
        await client.post(endpoint(claim), json=retry(expected_revision=2))
    ).status_code == 409


async def test_authorized_retry_failure_does_not_spawn_another_attempt(recovery_client):
    client, store, claim = recovery_client
    assert (
        await client.post(endpoint(claim), json=correction(claim))
    ).status_code == 200
    assert (
        await client.post(endpoint(claim), json=retry(expected_revision=1))
    ).status_code == 200
    retried = await store.experience.claim()
    await record_attempt(
        store.engine,
        retried,
        status="failed",
        usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
    )
    await store.experience.release(retried, error="known fixture parse failure")
    job = (await store.experience.jobs("report-1"))[0]
    assert job["status"] == "failed" and job["usage"]["total_tokens"] == 11
    assert await store.experience.claim() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"note": " "},
        {"input_tokens": True},
        {"output_tokens": -1},
        {"input_tokens": "5"},
        {"attempt_id": "missing"},
        {"unexpected": "field"},
    ],
)
async def test_invalid_corrections_leave_ledger_unchanged(recovery_client, changes):
    client, store, claim = recovery_client
    before = await store.experience.jobs("report-1")
    response = await client.post(endpoint(claim), json=correction(claim, **changes))
    assert response.status_code in (400, 422), response.text
    assert await store.experience.jobs("report-1") == before


async def test_retry_needs_charge_acknowledgment_and_inactive_job(recovery_client):
    client, store, claim = recovery_client
    assert (
        await client.post(
            endpoint(claim), json=retry(acknowledge_duplicate_charge=False)
        )
    ).status_code == 422
    assert (await client.post(endpoint(claim), json=retry())).status_code == 200
    assert (
        await client.post(endpoint(claim), json=correction(claim, expected_revision=1))
    ).status_code == 409
    await store.experience.claim()
    assert (
        await client.post(endpoint(claim), json=retry(expected_revision=1))
    ).status_code == 409


async def test_recovery_rejects_foreign_report_and_job(recovery_client):
    client, store, claim = recovery_client
    before = await store.experience.jobs("report-1")
    from tests.integration.test_experience_api import _create_report

    await _create_report(store, "foreign", "actor-2")
    response = await client.post(
        endpoint(claim).replace("report-1", "foreign"), json=correction(claim)
    )
    assert response.status_code == 404
    response = await client.post(
        endpoint(claim).replace(claim["id"], "missing"), json=correction(claim)
    )
    assert response.status_code == 404
    assert await store.experience.jobs("report-1") == before


async def test_two_operator_tabs_cannot_authorize_two_retries(recovery_client):
    client, store, claim = recovery_client
    responses = await asyncio.gather(
        client.post(endpoint(claim), json=retry()),
        client.post(endpoint(claim), json=retry()),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    job = (await store.experience.jobs("report-1"))[0]
    assert job["max_attempts"] == 2 and len(job["usage"]["recovery_audit"]) == 1
    assert await store.experience.claim() is not None
    assert await store.experience.claim() is None


async def test_retry_before_first_model_call_does_not_invent_legacy_usage(lease_stores):
    from research_copilot.runtime.retrospective_recovery import RecoveryRequest

    store, _ = lease_stores
    claim = await store.experience.claim()
    await store.experience.release(claim, error="pre-call fixture failure", retry=False)
    await ExperienceService(store).recover(
        "actor-1", "report-1", claim["id"], RecoveryRequest.model_validate(retry())
    )
    retried = await store.experience.claim()
    await record_attempt(
        store.engine,
        retried,
        status="completed",
        usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
    )
    await store.experience.complete(retried, [])
    job = (await store.experience.jobs("report-1"))[0]
    assert job["usage"]["unknown_attempts"] == 0
    assert "legacy_usage" not in job["usage"]
    assert len(job["usage"]["recovery_audit"]) == 1
