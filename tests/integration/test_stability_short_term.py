import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import ToolMessage
from filelock import FileLock

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.control import RunControl
from research_copilot.runtime.workflow import decide_next, dispatch_id
from research_copilot.sandbox.receipts import ExecutionService
from research_copilot.web.app import WorkbenchBackend, create_app
from tests.unit.test_product_workflow import state
from tests.integration.test_web_app import FakeSession, POLICY
from research_copilot.runtime.storage import SqlSessionStore
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
async def workbench(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    session = FakeSession(store)
    backend = WorkbenchBackend(
        session=session,
        store=store,
        actor_id="local",
        workspace_root=tmp_path,
        execution_policy=POLICY,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend=backend)),
        base_url="http://localhost",
    ) as client:
        try:
            yield client, backend
        finally:
            session.gate.set()
            await backend.runs.aclose()
            await engine.dispose()


@pytest.mark.asyncio
async def test_untrusted_writes(workbench):
    client, backend = workbench
    for headers, status in [
        ({"origin": "https://example.org"}, 403),
        ({"host": "rebind.example.org"}, 403),
        ({"content-type": "text/plain"}, 415),
    ]:
        response = await client.post(
            "/api/reports", headers=headers, json={"user_request": "q"}
        )
        assert response.status_code == status
    assert await backend.store.list_for_actor("local") == []


@pytest.mark.asyncio
async def test_single_report_for_duplicate_and_admission_before_write(workbench):
    client, backend = workbench
    from sqlalchemy import update
    from research_copilot.runtime.run_queue import run_queue_settings

    async with backend.store.engine.begin() as connection:
        await connection.execute(
            update(run_queue_settings).values(max_active=1, max_pending=1)
        )

    async def create(key, text="hello"):
        return await client.post(
            "/api/reports",
            headers={"Idempotency-Key": key},
            json={"user_request": text},
        )

    first, repeat = await asyncio.gather(create("first"), create("first"))
    assert first.status_code == repeat.status_code == 201
    rid = first.json()["report"]["id"]
    assert rid == repeat.json()["report"]["id"]
    assert (await create("first", "different")).status_code == 409
    await backend.session.entered.wait()
    assert (await create("second")).status_code == 201
    assert (await create("third")).status_code == 429
    assert len(await backend.store.list_for_actor("local")) == 2
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.post(f"/api/reports/{rid}/cancel")).status_code == 200


@pytest.mark.asyncio
async def test_unavailable_storage_degrades_health(workbench, monkeypatch):
    client, backend = workbench

    async def unavailable():
        raise TimeoutError

    monkeypatch.setattr(backend.store, "probe", unavailable, raising=False)
    assert (await client.get("/api/health")).json()["status"] == "degraded"
    assert (await client.get("/api/live")).status_code == 200


@pytest.mark.asyncio
async def test_provisional_same_value_bill():
    budget = TokenBudget(limit=100, budget_id="b")
    key = await budget.reserve(20)
    await budget.settle(key, 10, details={"usage_provisional": True})
    assert not budget.status()["usage_verified"]
    await budget.reconcile(key, 10)
    assert budget.status()["usage_verified"] and budget.requests[key]["reconciled"]
    assert budget.charged_tokens == 10
    await budget.reconcile(key, 10)
    with pytest.raises(ValueError, match="Conflicting"):
        await budget.reconcile(key, 11)


@pytest.mark.asyncio
async def test_sandbox_queue_deadline(tmp_path):
    service = ExecutionService(
        SimpleNamespace(workspace_root=tmp_path), control=RunControl(), budget=None
    )
    service.config = service.config.model_copy(
        update={"queue_seconds": 0.02, "concurrency": 1}
    )
    lock_dir = tmp_path / ".sandbox-locks"
    lock_dir.mkdir()
    with (
        FileLock(lock_dir / "slot-0.lock"),
        pytest.raises(TimeoutError, match="sandbox_queue_timeout"),
    ):
        async with asyncio.timeout(0.3), service._locks("report", "d"):
            pytest.fail("unexpected admission")
    async with service._locks("report", "d"):
        pass


def test_correction_bound_to_checkpoint_and_plan():
    from research_copilot.runtime.workflow import correction_candidate

    s = state()
    action = decide_next(s, "r")
    message = ToolMessage(
        content=json.dumps(
            {
                "error": "invalid_subagent_result",
                "dispatch_id": dispatch_id("r", action["id"]),
            }
        ),
        tool_call_id=action["id"],
        status="error",
    )
    assert correction_candidate(s, "r", [message]) == action["id"]
    s.role_data["executions"] = {"unknown": {"status": "unknown"}}
    assert correction_candidate(s, "r", [message]) is None


def test_budget_corrected_reviewer_can_be_corrected_again():
    from research_copilot.runtime.workflow import command, correction_candidate
    from tests.unit.test_product_workflow import finish, ref

    s = state()
    finish(s, decide_next(s, "r"))
    finish(s, decide_next(s, "r"))
    s.artifacts["analysis"] = ref("analysis")
    s.approved["analysis"] = s.artifacts["analysis"]
    finish(s, decide_next(s, "r"))
    s.artifacts["report"] = ref("report")
    original = decide_next(s, "r")
    assert original["args"]["subagent_type"] == "reviewer"
    s.role_data["dispatch_recoveries"] = {
        original["id"]: {"authorization_id": "auth-1"}
    }
    corrected_id = command("task", {}, [original["id"], "auth-1"])["id"]
    assert decide_next(s, "r")["id"] == corrected_id
    message = ToolMessage(
        content=json.dumps(
            {
                "error": "stage_budget_exhausted",
                "dispatch_id": dispatch_id("r", corrected_id),
            }
        ),
        tool_call_id=corrected_id,
        status="error",
    )
    assert correction_candidate(s, "r", [message]) == corrected_id


def test_correction_accepts_reviewer_stage_budget_exhausted():
    from research_copilot.runtime.workflow import correction_candidate
    from tests.unit.test_product_workflow import finish, ref

    s = state()
    finish(s, decide_next(s, "r"))
    finish(s, decide_next(s, "r"))
    s.artifacts["analysis"] = ref("analysis")
    s.approved["analysis"] = s.artifacts["analysis"]
    finish(s, decide_next(s, "r"))
    s.artifacts["report"] = ref("report")
    action = decide_next(s, "r")
    assert action["args"]["subagent_type"] == "reviewer"
    message = ToolMessage(
        content=json.dumps(
            {
                "error": "stage_budget_exhausted",
                "dispatch_id": dispatch_id("r", action["id"]),
            }
        ),
        tool_call_id=action["id"],
        status="error",
    )
    assert correction_candidate(s, "r", [message]) == action["id"]


def test_correction_accepts_writer_repair_exhausted_with_approved_analysis():
    from research_copilot.runtime.workflow import correction_candidate
    from tests.unit.test_product_workflow import finish, ref

    s = state()
    finish(s, decide_next(s, "r"))
    finish(s, decide_next(s, "r"))
    s.artifacts["analysis"] = ref("analysis")
    s.approved["analysis"] = s.artifacts["analysis"]
    action = decide_next(s, "r")
    assert action["args"]["subagent_type"] == "writer"
    message = ToolMessage(
        content=json.dumps(
            {
                "error": "role_result_repair_exhausted",
                "dispatch_id": dispatch_id("r", action["id"]),
            }
        ),
        tool_call_id=action["id"],
        status="error",
    )
    assert correction_candidate(s, "r", [message]) == action["id"]


def test_correction_accepts_repair_exhausted_dispatch():
    from research_copilot.runtime.workflow import correction_candidate

    s = state()
    action = decide_next(s, "r")
    message = ToolMessage(
        content=json.dumps(
            {
                "error": "role_result_repair_exhausted",
                "dispatch_id": dispatch_id("r", action["id"]),
            }
        ),
        tool_call_id=action["id"],
        status="error",
    )
    assert correction_candidate(s, "r", [message]) == action["id"]


@pytest.mark.asyncio
async def test_old_correction_endpoint_cannot_mint_a_native_dispatch(workbench):
    from research_copilot.runtime.session import SessionService, SessionRecord
    from research_copilot.runtime.identity import RUNTIME_VERSION
    from research_copilot.contracts import RunContext
    from unittest.mock import Mock

    client, backend = workbench
    record = SessionRecord(
        "r",
        "local",
        RUNTIME_VERSION,
        backend.policy_version,
        "b",
        run_id="run",
        status="failed",
    )
    await backend.store.create(record, TokenBudget(limit=100, budget_id="b").snapshot())
    ctx = RunContext("local", "r", "run", "b", backend.policy_version)
    factory = Mock(side_effect=AssertionError("Correction must not construct a graph"))
    backend.session = SessionService(
        store=backend.store,
        agent_factory=factory,
        policy_version=backend.policy_version,
    )
    from research_copilot.web.runs import DurableRuns

    backend.runs = DurableRuns(backend.session, backend.store, "local")
    before = (await backend.store.read_control(ctx)).model_dump()
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/reports/r/correct", json={"dispatch_call_id": "wf-" + "a" * 32}
            )
            for _ in range(2)
        )
    )
    assert all(r.status_code == 409 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    assert (await backend.store.read_control(ctx)).model_dump() == before
    assert (await backend.store.load_budget(ctx)).calls == 0
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_created_without_run_can_start_from_workspace(workbench):
    from research_copilot.runtime.session import SessionRecord
    from research_copilot.runtime.identity import RUNTIME_VERSION

    client, backend = workbench
    record = SessionRecord(
        "unstarted",
        "local",
        RUNTIME_VERSION,
        backend.policy_version,
        "b",
        user_request="recover original input",
    )
    await backend.store.create(record, TokenBudget(limit=100, budget_id="b").snapshot())
    page = (await client.get("/api/reports/unstarted")).json()
    assert "resume" in [a["key"] for a in page["actions"]]
    assert (await client.post("/api/reports/unstarted/resume")).status_code == 200
    await asyncio.wait_for(backend.session.entered.wait(), 1)
    assert (await backend.store.load("unstarted", "local")).run_id


@pytest.mark.asyncio
async def test_correction_cannot_start_another_paid_source_request():
    from research_copilot.subagents.collection import begin_source_call
    from tests.unit.test_collection_convergence import environment

    mw, req, _ = environment({"correction_of": "original", "evidence_ids": ["E-1"]})
    scope, _ = await mw.services.scope(None, "searcher")
    call = {"id": "new", "name": "web_search", "args": {"query": "again"}}
    assert (
        await begin_source_call(mw.services, None, scope, call)
        == "correction_existing_sources_only"
    )


@pytest.mark.asyncio
async def test_readiness_has_bounded_probe_and_recovers(workbench, monkeypatch):
    client, backend = workbench
    from unittest.mock import AsyncMock

    probe = AsyncMock(side_effect=[TimeoutError(), None])
    monkeypatch.setattr(backend.store, "probe", probe)
    assert (await client.get("/api/ready")).status_code == 503
    assert (await client.get("/api/health")).json()["status"] == "degraded"
    assert probe.await_count == 1
    await asyncio.sleep(1.05)
    assert (await client.get("/api/ready")).status_code == 200
    assert probe.await_count == 2


@pytest.mark.asyncio
async def test_run_id_persisted_before_first_checkpoint_uses_original_budget(workbench):
    from research_copilot.runtime.identity import RUNTIME_VERSION
    from research_copilot.runtime.session import SessionRecord, SessionService
    from unittest.mock import AsyncMock

    _, backend = workbench
    record = SessionRecord(
        "uncheckpointed",
        "local",
        RUNTIME_VERSION,
        backend.policy_version,
        "ledger",
        run_id="original-run",
        status="running",
        user_request="original question",
    )
    await backend.store.create(
        record, TokenBudget(limit=100, budget_id="ledger").snapshot()
    )
    snapshot = SimpleNamespace(
        interrupts=(), next=(), tasks=(), values={}, created_at=None
    )
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot))
    service = SessionService(
        store=backend.store,
        agent_factory=lambda *_: graph,
        policy_version=backend.policy_version,
    )
    service._invoke = AsyncMock()
    await service.resume(record.report_id, record.actor_id)
    called_record, initial = service._invoke.await_args.args
    assert (
        called_record == record
        and initial["messages"][0]["content"] == record.user_request
    )
    ctx = service._context(record)
    budget = await backend.store.load_budget(ctx)
    await budget.reserve(10)
    with pytest.raises(ValueError, match="首个检查点"):
        await service.resume(record.report_id, record.actor_id)
    assert service._invoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_historical_checkpoint_rejected_before_graph_or_queue_execution(
    workbench, queued
):
    from unittest.mock import Mock
    from research_copilot.contracts import RunContext
    from research_copilot.runtime.identity import RUNTIME_VERSION
    from research_copilot.runtime.session import SessionRecord, SessionService
    from research_copilot.runtime.run_queue import RunQueue, QueueConflict

    _, backend = workbench
    record = SessionRecord(
        "graph",
        "local",
        RUNTIME_VERSION,
        backend.policy_version,
        "b",
        run_id="same-run",
        status="failed",
        user_request="collect",
    )
    await backend.store.create(
        record,
        TokenBudget(limit=100, budget_id="b").snapshot(),
        workflow_version="products-v1",
    )
    ctx = RunContext("local", "graph", "same-run", "b", backend.policy_version)
    before = (await backend.store.load_budget(ctx)).snapshot()
    factory = Mock(side_effect=AssertionError("Do not construct historical graph"))
    service = SessionService(
        store=backend.store,
        agent_factory=factory,
        policy_version=backend.policy_version,
    )
    with pytest.raises((PermissionError, QueueConflict), match="read-only"):
        if queued:
            await RunQueue(backend.store.engine).enqueue(
                report_id="graph",
                actor_id="local",
                operation_id="old-correction",
                kind="correct",
                payload={"dispatch_call_id": "old-call"},
            )
        else:
            await service.resume("graph", "local")
    assert (await service.get_status("graph", "local")).status == "read_only"
    assert (await backend.store.load_budget(ctx)).snapshot() == before
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_same_key_after_persist_before_launch_starts_existing_report(workbench):
    from hashlib import sha256
    from research_copilot.runtime.session import SessionRecord
    from research_copilot.runtime.identity import RUNTIME_VERSION

    client, backend = workbench
    rid = sha256(b"local\x00retry").hexdigest()[:32]
    record = SessionRecord(
        rid, "local", RUNTIME_VERSION, backend.policy_version, rid, user_request="q"
    )
    await backend.store.create(record, TokenBudget(limit=42, budget_id=rid).snapshot())
    response = await client.post(
        "/api/reports", headers={"Idempotency-Key": "retry"}, json={"user_request": "q"}
    )
    assert response.status_code == 201 and response.json()["report"]["id"] == rid
    # This checks durable retry semantics, not a sub-second latency SLA. Allow
    # multiple worker polling cycles plus SQLite I/O on Windows under suite load.
    await asyncio.wait_for(backend.session.entered.wait(), 3)
    assert len(await backend.store.list_for_actor("local")) == 1
