import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.runtime.autonomy import stop_reason
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.dispatch_repair import repair_dispatch_result
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from tests.unit.test_orchestration import context


@pytest.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        TokenBudget(limit=1_000_000, budget_id="budget-1").snapshot(),
    )
    try:
        yield store
    finally:
        await engine.dispose()


async def _fail_repair_dispatch(store):
    def operation(state):
        state.dispatches["dispatch-1"] = {
            "role": "writer",
            "description": "write report",
            "inputs": {},
            "repair_of": "dispatch-0",
        }
        state.role_data["dispatch_failures"] = {
            "dispatch-1": {
                "code": "role_result_repair_exhausted",
                "repairable": True,
            }
        }

    await store.mutate_control(context(), operation)


async def test_repair_unblocks_stop_reason_and_is_audited(store):
    await _fail_repair_dispatch(store)
    state = await store.read_control(context())
    assert "role_result_repair_exhausted" in stop_reason(state)

    receipt = await repair_dispatch_result(
        store,
        report_id="report-1",
        actor_id="actor-1",
        dispatch_id="dispatch-1",
        note="sections 3/7 exceeded 675 chars and 76.85%/79.31% were unregistered; re-brief caps sections and forbids the ratios",
    )
    assert receipt["role"] == "writer"
    assert receipt["failure_code"] == "role_result_repair_exhausted"

    state = await store.read_control(context())
    failure = state.role_data["dispatch_failures"]["dispatch-1"]
    assert failure["repaired_by"] == receipt["receipt_id"]
    assert stop_reason(state) is None
    entry = state.role_data["dispatch_repairs"][-1]
    assert entry["note"].startswith("sections 3/7 exceeded")
    assert entry["confirmed_at"]
    # The repair clears the failure without touching usage or limits.
    assert state.dispatches["dispatch-1"]["repair_of"] == "dispatch-0"
    budget = await store.load_budget(context())
    assert budget.limit == 1_000_000


async def test_repair_rejects_unknown_missing_or_wrong_code(store):
    await _fail_repair_dispatch(store)
    with pytest.raises(ValueError, match="Unknown dispatch"):
        await repair_dispatch_result(
            store,
            report_id="report-1",
            actor_id="actor-1",
            dispatch_id="missing",
            note="typo",
        )
    with pytest.raises(ValueError, match="already repaired"):
        await repair_dispatch_result(
            store,
            report_id="report-1",
            actor_id="actor-1",
            dispatch_id="dispatch-1",
            note="first repair",
        )
        await repair_dispatch_result(
            store,
            report_id="report-1",
            actor_id="actor-1",
            dispatch_id="dispatch-1",
            note="second repair",
        )

    def add_budget_failure(state):
        state.dispatches["dispatch-2"] = {
            "role": "writer",
            "description": "write report again",
            "inputs": {},
        }
        state.role_data["dispatch_failures"]["dispatch-2"] = {
            "code": "stage_budget_exhausted",
            "repairable": False,
        }

    await store.mutate_control(context(), add_budget_failure)
    with pytest.raises(ValueError, match="matching lever"):
        await repair_dispatch_result(
            store,
            report_id="report-1",
            actor_id="actor-1",
            dispatch_id="dispatch-2",
            note="budget codes belong to the budget lever",
        )
    with pytest.raises(ValueError, match="note"):
        await repair_dispatch_result(
            store,
            report_id="report-1",
            actor_id="actor-1",
            dispatch_id="dispatch-2",
            note="  ",
        )
    with pytest.raises(ValueError, match="actor"):
        await repair_dispatch_result(
            store,
            report_id="report-1",
            actor_id="  ",
            dispatch_id="dispatch-2",
            note="actor is required",
        )
    state = await store.read_control(context())
    assert not state.role_data["dispatch_failures"]["dispatch-2"].get("repaired_by")


async def test_repair_accepts_invalid_subagent_result(store):
    def fail_review_dispatch(state):
        state.dispatches["dispatch-3"] = {
            "role": "reviewer",
            "description": "review report",
            "inputs": {},
            "repair_of": "dispatch-1",
        }
        state.role_data["dispatch_failures"] = {
            "dispatch-3": {
                "code": "invalid_subagent_result",
                "repairable": True,
                "message": "Task failed: ValueError: Request recovery requires retry authorization",
            }
        }

    await store.mutate_control(context(), fail_review_dispatch)
    state = await store.read_control(context())
    # A repair dispatch with its own unrepaired failure blocks the workflow.
    assert "Automatic repair stopped" in stop_reason(state)

    receipt = await repair_dispatch_result(
        store,
        report_id="report-1",
        actor_id="actor-1",
        dispatch_id="dispatch-3",
        note="reviewer branch crashed on operation_id dedup collision with the internal summarizer call; discriminator added in code",
    )
    assert receipt["failure_code"] == "invalid_subagent_result"
    state = await store.read_control(context())
    failure = state.role_data["dispatch_failures"]["dispatch-3"]
    assert failure["repaired_by"] == receipt["receipt_id"]
    assert stop_reason(state) is None
    entry = state.role_data["dispatch_repairs"][-1]
    assert entry["failure_code"] == "invalid_subagent_result"
    assert entry["dispatch_id"] == "dispatch-3"


@pytest.mark.asyncio
async def test_repair_exhaustion_records_the_concrete_validation_error(tmp_path):
    from unittest.mock import AsyncMock

    from research_copilot.orchestration.middleware import DispatchMiddleware
    from research_copilot.runtime.business import BusinessCoordinator
    from research_copilot.runtime.control import RunControl
    from research_copilot.runtime.identity import dispatch_identity
    from research_copilot.subagents.contracts import BusinessInputError
    from research_copilot.subagents.middleware import RoleRepairExhausted
    from research_copilot.subagents.services import RoleServices
    from tests.unit.test_business import ControlRepository

    repository = ControlRepository()
    repository.state.role_data["workflow_version"] = "products-v2"
    business = BusinessCoordinator(repository)
    services = RoleServices(
        repository=repository, business=business, workspace_root=tmp_path
    )
    await business.set_report_scope(
        context(),
        {"report_type": "company_report", "objective": "Write", "as_of": "2025-04-30"},
    )

    def seed_analysis(state):
        from research_copilot.contracts import ArtifactRef

        state.artifacts["analysis"] = ArtifactRef(
            artifact_id="analysis-1", version="1", content_hash="b" * 64
        )

    await repository.mutate_control(context(), seed_analysis)
    ident = dispatch_identity("report-1", "call-9")
    brief = TaskBrief(
        task_id="write", role="writer", objective="write", as_of="2025-04-30"
    )
    await services.prepare_dispatch(
        context(),
        Dispatch(ident, "writer", f"/workspace/tasks/{ident}/"),
        brief.model_dump_json(),
    )

    async def handler(_):
        raise RoleRepairExhausted("Role result repair limit reached") from (
            BusinessInputError(
                "uncovered numeric claim: 1 | context: 1—3月累计营业收入"
            )
        )

    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context()),
        tool_call={
            "name": "task",
            "id": "call-9",
            "args": {"subagent_type": "writer", "description": brief.model_dump_json()},
        },
    )
    request.override = lambda **kwargs: SimpleNamespace(
        **{**vars(request), **kwargs}
    )
    result = await DispatchMiddleware(
        business, RunControl(), "report-1"
    ).awrap_tool_call(request, AsyncMock(side_effect=handler))
    assert result.status == "error"
    payload = json.loads(result.content)
    assert payload["error"] == "role_result_repair_exhausted"
    assert "uncovered numeric claim: 1" in payload["detail"]
    # The failure record is diagnosable without the subagent conversation.
    failure = repository.state.role_data["dispatch_failures"][ident]
    assert failure["code"] == "role_result_repair_exhausted"
    assert "uncovered numeric claim: 1" in failure["message"]
    assert failure["repairable"] is True
