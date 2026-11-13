import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.collection_recovery import (
    _work_group,
    recover_collection,
)
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from tests.unit.test_orchestration import context


@pytest.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'collection.db'}")
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


async def _seed(store, *, group, other_group, review_id=None):
    def operation(state):
        state.artifacts["scope"] = ArtifactRef(
            artifact_id="scope:report-1", version="1", content_hash="a" * 64
        )
        state.role_data["collection_work"] = {
            group: {
                "calls": {"d1:call": "identity"},
                "max_source_calls": 8,
                "max_no_progress_calls": 2,
                "no_progress_calls": 2,
            },
            other_group: {
                "calls": {},
                "max_source_calls": 8,
                "max_no_progress_calls": 2,
                "no_progress_calls": 2,
            },
        }
        state.dispatches["d1"] = {"role": "searcher", "work_group": group}
        state.dispatches["d2"] = {"role": "searcher", "work_group": other_group}
        state.role_data["scopes"] = {
            "d1": {"no_progress_calls": 2, "collection_stop_reason": "no_progress"},
            "d2": {"no_progress_calls": 2, "collection_stop_reason": "no_progress"},
        }

    await store.mutate_control(context(), operation)


async def test_recovery_resets_only_the_requested_review_group(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    group = _work_group(scope, None)
    await _seed(store, group=group, other_group="other-group")

    receipt = await recover_collection(
        store,
        report_id="report-1",
        actor_id="actor-1",
        review_id=None,
        note="host adapter defect produced a false no-progress block",
    )
    assert receipt["changed"] is True
    assert receipt["previous_no_progress_calls"] == 2
    assert receipt["reset_dispatches"] == ["d1"]

    state = await store.read_control(context())
    assert state.role_data["collection_work"][group]["no_progress_calls"] == 0
    assert state.role_data["collection_work"]["other-group"]["no_progress_calls"] == 2
    assert state.role_data["scopes"]["d1"]["no_progress_calls"] == 0
    assert "collection_stop_reason" not in state.role_data["scopes"]["d1"]
    assert state.role_data["scopes"]["d2"]["no_progress_calls"] == 2
    assert state.role_data["collection_work"][group]["calls"]
    audit = state.role_data["collection_recoveries"][-1]
    assert audit["note"].startswith("host adapter defect")
    assert audit["review_id"] is None


async def test_recovery_is_idempotent(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    group = _work_group(scope, None)
    await _seed(store, group=group, other_group="other-group")
    arguments = {
        "report_id": "report-1",
        "actor_id": "actor-1",
        "review_id": None,
        "note": "reset",
    }
    await recover_collection(store, **arguments)
    second = await recover_collection(store, **arguments)
    assert second["changed"] is False
    state = await store.read_control(context())
    assert len(state.role_data["collection_recoveries"]) == 1


async def test_recovery_uses_the_rework_review_group(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    group = _work_group(scope, "review-1")
    await _seed(store, group=group, other_group=_work_group(scope, None))
    receipt = await recover_collection(
        store,
        report_id="report-1",
        actor_id="actor-1",
        review_id="review-1",
        note="rework round reset",
    )
    assert receipt["review_id"] == "review-1"
    assert receipt["changed"] is True


async def test_unknown_group_is_rejected(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    await _seed(store, group=_work_group(scope, None), other_group="other-group")
    with pytest.raises(ValueError, match="work group"):
        await recover_collection(
            store,
            report_id="report-1",
            actor_id="actor-1",
            review_id="review-missing",
            note="nothing to reset",
        )


async def test_recovery_requires_a_reason(store):
    scope = ArtifactRef(
        artifact_id="scope:report-1", version="1", content_hash="a" * 64
    )
    await _seed(store, group=_work_group(scope, None), other_group="other-group")
    with pytest.raises(ValueError, match="note"):
        await recover_collection(
            store,
            report_id="report-1",
            actor_id="actor-1",
            review_id=None,
            note=" ",
        )
