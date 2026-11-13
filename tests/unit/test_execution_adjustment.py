import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef
from research_copilot.runtime.budget import BudgetExceeded, TokenBudget
from research_copilot.runtime.execution_adjustment import extend_executions
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.runtime.work_group import work_group_id
from research_copilot.sandbox.execution_policy import check_execution_policy
from tests.unit.test_orchestration import context


@pytest.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'executions.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        TokenBudget(limit=100_000, budget_id="budget-1").snapshot(),
    )
    try:
        yield store
    finally:
        await engine.dispose()


def _seed(store, *, group):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )

    def operation(state):
        state.artifacts["scope"] = scope
        state.dispatches["d1"] = {"role": "analysist", "work_group": group}
        state.role_data["scopes"] = {
            "d1": {
                "task_brief": {
                    "max_executions": 2,
                    "calculation_mode": "custom",
                    "execution_purposes": ["compute_metrics", "compute_metrics_repair"],
                },
                "analysis_execution_limit": 3,
            }
        }
        state.role_data["executions"] = {
            "e1": {"execution_id": "e1", "dispatch_id": "d1", "status": "registered"},
            "e2": {
                "execution_id": "e2",
                "dispatch_id": "d1",
                "status": "failed",
                "response": {"error": {"code": "script_error"}},
            },
            "e3": {
                "execution_id": "e3",
                "dispatch_id": "d1",
                "status": "failed",
                "response": {"error": {"code": "script_error"}},
            },
        }
        state.role_data["dispatch_failures"] = {
            "d1": {"code": "sandbox_execution_limit", "repairable": False}
        }

    return store.mutate_control(context(), operation)


async def test_extension_adds_attempts_and_repairs_the_failure(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    group = work_group_id(scope, None)
    await _seed(store, group=group)

    receipt = await extend_executions(
        store,
        report_id="report-1",
        actor_id="actor-1",
        review_id=None,
        additional=2,
        note="existing metric computation is reusable; allow one retry",
    )
    assert receipt["prior_execution_ids"] == ["e1", "e2", "e3"]
    assert receipt["additional_attempts"] == 2
    assert receipt["repaired_dispatches"] == ["d1"]

    state = await store.read_control(context())
    failure = state.role_data["dispatch_failures"]["d1"]
    assert failure["repaired_by"] == receipt["receipt_id"]
    grants = state.role_data["execution_extensions"]
    assert grants[-1]["reason"].startswith("existing metric computation")

    def policy(extensions):
        return check_execution_policy(
            "d1",
            state.role_data["scopes"],
            state.dispatches,
            list(state.role_data["executions"].values()),
            20,
            extensions=extensions,
        )

    with pytest.raises(BudgetExceeded) as blocked:
        policy([])
    assert blocked.value.details["code"] == "sandbox_execution_limit"
    assert len(policy(grants)) == 3


async def test_extension_repairs_an_expired_dispatch_window(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    group = work_group_id(scope, None)
    await _seed(store, group=group)

    def expire(state):
        state.role_data["dispatch_failures"]["d1"]["code"] = (
            "sandbox_deadline_exhausted"
        )

    await store.mutate_control(context(), expire)
    receipt = await extend_executions(
        store,
        report_id="report-1",
        actor_id="actor-1",
        review_id=None,
        additional=1,
        note="host paused longer than the dispatch window",
    )
    assert receipt["repaired_dispatches"] == ["d1"]
    state = await store.read_control(context())
    assert (
        state.role_data["dispatch_failures"]["d1"]["repaired_by"]
        == receipt["receipt_id"]
    )


async def test_extension_cannot_be_repeated_without_progress(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    await _seed(store, group=work_group_id(scope, None))
    arguments = {
        "report_id": "report-1",
        "actor_id": "actor-1",
        "review_id": None,
        "additional": 2,
        "note": "retry analysis",
    }
    await extend_executions(store, **arguments)
    with pytest.raises(ValueError, match="only be extended"):
        await extend_executions(store, **arguments)


async def test_unknown_round_and_invalid_inputs_are_rejected(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    await _seed(store, group=work_group_id(scope, None))
    with pytest.raises(ValueError, match="No sandbox executions"):
        await extend_executions(
            store,
            report_id="report-1",
            actor_id="actor-1",
            review_id="review-other",
            additional=1,
            note="nothing to extend",
        )
    with pytest.raises(ValueError, match="note"):
        await extend_executions(
            store,
            report_id="report-1",
            actor_id="actor-1",
            review_id=None,
            additional=1,
            note=" ",
        )
    with pytest.raises(ValueError, match="between 1 and 20"):
        await extend_executions(
            store,
            report_id="report-1",
            actor_id="actor-1",
            review_id=None,
            additional=0,
            note="invalid",
        )
