import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.budget_adjustment import extend_stage_budget
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from tests.unit.test_orchestration import context


async def _create_store(tmp_path, budget):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'adjust.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        budget.snapshot(),
    )
    return engine, store


@pytest.fixture
async def store(tmp_path):
    engine, store = await _create_store(
        tmp_path,
        TokenBudget(
            limit=1_000_000,
            budget_id="budget-1",
            stage_limits={
                "analysist": {"calls": 44, "tokens": 100_000},
                "searcher": {"calls": 40, "tokens": 50_000},
            },
        ),
    )
    try:
        yield store
    finally:
        await engine.dispose()


async def _fail_analysis(store):
    def operation(state):
        state.artifacts["scope"] = ArtifactRef(
            artifact_id="scope:report-1", version="1", content_hash="a" * 64
        )
        state.dispatches["dispatch-1"] = {
            "role": "analysist",
            "work_group": "group-1",
            "inputs": {},
        }
        state.role_data["dispatch_failures"] = {
            "dispatch-1": {
                "code": "stage_budget_exhausted",
                "repairable": False,
            }
        }

    await store.mutate_control(context(), operation)


async def test_extension_is_audited_and_unblocks_resume(store):
    from research_copilot.runtime.autonomy import stop_reason

    await _fail_analysis(store)
    receipt = await extend_stage_budget(
        store,
        report_id="report-1",
        actor_id="actor-1",
        stage="analysist",
        tokens=240_000,
        note="CATL acceptance run needs analysis wrap-up",
    )
    assert receipt["previous_tokens"] == 100_000
    assert receipt["tokens"] == 240_000
    assert receipt["repaired_dispatches"] == ["dispatch-1"]

    budget = await store.load_budget(context())
    assert budget.stage_limits["analysist"]["tokens"] == 240_000
    state = await store.read_control(context())
    assert (
        state.role_data["dispatch_failures"]["dispatch-1"]["repaired_by"]
        == receipt["receipt_id"]
    )
    adjustment = state.role_data["budget_adjustments"][-1]
    assert adjustment["note"] == "CATL acceptance run needs analysis wrap-up"
    assert adjustment["confirmed_at"]
    assert stop_reason(state) is None
    # The raised cap is effective for the resumed run, not only recorded.
    assert await budget.reserve(150_000, stage="analysist")


async def test_call_limit_extension_is_audited_and_effective(store):
    await _fail_analysis(store)
    receipt = await extend_stage_budget(
        store,
        report_id="report-1",
        actor_id="actor-1",
        stage="analysist",
        calls=60,
        note="analysist call cap reached with token headroom remaining",
    )
    assert receipt["previous_calls"] == 44
    assert receipt["calls"] == 60
    assert "tokens" not in receipt

    budget = await store.load_budget(context())
    assert budget.stage_limits["analysist"]["calls"] == 60
    assert budget.stage_limits["analysist"]["tokens"] == 100_000
    state = await store.read_control(context())
    assert (
        state.role_data["dispatch_failures"]["dispatch-1"]["repaired_by"]
        == receipt["receipt_id"]
    )


async def test_token_and_call_limits_can_be_extended_together(store):
    receipt = await extend_stage_budget(
        store,
        report_id="report-1",
        actor_id="actor-1",
        stage="analysist",
        tokens=240_000,
        calls=60,
        note="wrap-up needs both more calls and tokens",
    )
    assert receipt["tokens"] == 240_000
    assert receipt["calls"] == 60
    budget = await store.load_budget(context())
    assert budget.stage_limits["analysist"] == {"calls": 60, "tokens": 240_000}


@pytest.mark.parametrize(
    "stage,tokens,match",
    [
        ("writer", 500_000, "Unknown budget stage"),
        ("analysist", 100_000, "only be extended"),
        ("analysist", 1_000_001, "global token limit"),
    ],
)
async def test_invalid_extension_leaves_the_ledger_unchanged(
    store, stage, tokens, match
):
    with pytest.raises(ValueError, match=match):
        await extend_stage_budget(
            store,
            report_id="report-1",
            actor_id="actor-1",
            stage=stage,
            tokens=tokens,
            note="invalid attempt",
        )
    budget = await store.load_budget(context())
    assert budget.stage_limits["analysist"]["tokens"] == 100_000
    assert not (await store.read_control(context())).role_data.get("budget_adjustments")


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"calls": 44}, "only be extended"),
        ({"calls": 0}, "positive integer"),
        ({}, "token or call limit"),
    ],
)
async def test_invalid_call_extension_is_rejected(store, kwargs, match):
    with pytest.raises(ValueError, match=match):
        await extend_stage_budget(
            store,
            report_id="report-1",
            actor_id="actor-1",
            stage="analysist",
            note="invalid attempt",
            **kwargs,
        )
    budget = await store.load_budget(context())
    assert budget.stage_limits["analysist"]["calls"] == 44
    assert budget.stage_limits["analysist"]["tokens"] == 100_000


async def test_global_call_exhaustion_is_not_masked_by_stage_extension(tmp_path):
    engine, store = await _create_store(
        tmp_path,
        TokenBudget(
            limit=200_000,
            budget_id="budget-1",
            call_limit=100,
            calls=100,
            stage_limits={"analysist": {"calls": 44, "tokens": 100_000}},
        ),
    )
    try:
        with pytest.raises(ValueError, match="Global model call budget"):
            await extend_stage_budget(
                store,
                report_id="report-1",
                actor_id="actor-1",
                stage="analysist",
                calls=60,
                note="wrong lever",
            )
    finally:
        await engine.dispose()


async def test_extension_requires_a_reason(store):
    with pytest.raises(ValueError, match="note"):
        await extend_stage_budget(
            store,
            report_id="report-1",
            actor_id="actor-1",
            stage="analysist",
            tokens=240_000,
            note="   ",
        )


async def test_global_exhaustion_is_not_masked_by_stage_extension(tmp_path):
    engine, store = await _create_store(
        tmp_path,
        TokenBudget(
            limit=200_000,
            budget_id="budget-1",
            charged_tokens=200_000,
            stage_limits={"analysist": {"calls": 44, "tokens": 100_000}},
        ),
    )
    try:
        with pytest.raises(ValueError, match="Global token budget"):
            await extend_stage_budget(
                store,
                report_id="report-1",
                actor_id="actor-1",
                stage="analysist",
                tokens=150_000,
                note="wrong lever",
            )
    finally:
        await engine.dispose()


async def test_global_extension_is_audited_and_effective(tmp_path):
    from research_copilot.runtime.budget_adjustment import extend_global_budget

    engine, store = await _create_store(
        tmp_path,
        TokenBudget(
            limit=1_000_000,
            budget_id="budget-1",
            call_limit=160,
            charged_tokens=990_000,
            calls=158,
            stage_limits={"analysist": {"calls": 80, "tokens": 400_000}},
        ),
    )
    try:
        def fail(state):
            state.role_data["dispatch_failures"] = {
                "dispatch-1": {
                    "code": "task_token_budget_exhausted",
                    "repairable": False,
                }
            }

        await store.mutate_control(context(), fail)
        receipt = await extend_global_budget(
            store,
            report_id="report-1",
            actor_id="actor-1",
            tokens=2_000_000,
            calls=220,
            note="acceptance run needs writer and reviewer headroom",
        )
        assert receipt["previous_tokens"] == 1_000_000
        assert receipt["previous_calls"] == 160
        assert receipt["repaired_dispatches"] == ["dispatch-1"]

        budget = await store.load_budget(context())
        assert budget.limit == 2_000_000
        assert budget.call_limit == 220
        state = await store.read_control(context())
        assert (
            state.role_data["dispatch_failures"]["dispatch-1"]["repaired_by"]
            == receipt["receipt_id"]
        )
        adjustment = state.role_data["budget_adjustments"][-1]
        assert adjustment["note"] == "acceptance run needs writer and reviewer headroom"
        assert adjustment["stage"] is None
        # The raised cap is effective for the resumed run, not only recorded.
        assert await budget.reserve(400_000, stage="analysist")
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"tokens": 1_000_000}, "only be extended"),
        ({"tokens": 0}, "positive integer"),
        ({"calls": 160}, "only be extended"),
        ({}, "token or call limit"),
    ],
)
async def test_invalid_global_extension_leaves_the_ledger_unchanged(
    tmp_path, kwargs, match
):
    from research_copilot.runtime.budget_adjustment import extend_global_budget

    engine, store = await _create_store(
        tmp_path,
        TokenBudget(
            limit=1_000_000,
            budget_id="budget-1",
            call_limit=160,
            stage_limits={"analysist": {"calls": 44, "tokens": 100_000}},
        ),
    )
    try:
        with pytest.raises(ValueError, match=match):
            await extend_global_budget(
                store,
                report_id="report-1",
                actor_id="actor-1",
                note="invalid attempt",
                **kwargs,
            )
        budget = await store.load_budget(context())
        assert budget.limit == 1_000_000
        assert budget.call_limit == 160
        assert not (await store.read_control(context())).role_data.get(
            "budget_adjustments"
        )
    finally:
        await engine.dispose()


async def test_global_extension_cannot_fall_below_stage_limits(tmp_path):
    from research_copilot.runtime.budget_adjustment import extend_global_budget

    engine, store = await _create_store(
        tmp_path,
        TokenBudget(
            limit=1_000_000,
            budget_id="budget-1",
            call_limit=160,
            # Deliberately mis-seeded ledger: stage caps exceed the global caps,
            # so raising the global limit must refuse to bless the inconsistency.
            stage_limits={"analysist": {"calls": 400, "tokens": 1_500_000}},
        ),
    )
    try:
        with pytest.raises(ValueError, match="below stage limit"):
            await extend_global_budget(
                store,
                report_id="report-1",
                actor_id="actor-1",
                tokens=1_200_000,
                note="stage caps still exceed this global cap",
            )
        with pytest.raises(ValueError, match="below stage limit"):
            await extend_global_budget(
                store,
                report_id="report-1",
                actor_id="actor-1",
                calls=220,
                note="stage call cap still exceeds this global cap",
            )
        budget = await store.load_budget(context())
        assert budget.limit == 1_000_000
        assert budget.call_limit == 160
    finally:
        await engine.dispose()


async def test_global_extension_requires_a_reason(store):
    from research_copilot.runtime.budget_adjustment import extend_global_budget

    with pytest.raises(ValueError, match="note"):
        await extend_global_budget(
            store,
            report_id="report-1",
            actor_id="actor-1",
            tokens=2_000_000,
            note="   ",
        )


async def _branch_store(tmp_path):
    engine, store = await _create_store(
        tmp_path,
        TokenBudget(
            limit=1_000_000,
            budget_id="budget-1",
            stage_limits={
                "reviewer": {
                    "calls": 22,
                    "tokens": 750_000,
                    "branches": {"quality": 16, "compliance": 6},
                }
            },
        ),
    )
    return engine, store


async def test_branch_call_extension_is_audited_and_effective(tmp_path):
    from research_copilot.runtime.budget import BudgetExceeded

    engine, store = await _branch_store(tmp_path)
    try:
        budget = await store.load_budget(context())
        for _ in range(16):
            key = await budget.reserve(1_000, stage="reviewer", branch="quality")
            await budget.settle(key, 500)
        with pytest.raises(BudgetExceeded, match="Review branch call budget"):
            await budget.reserve(1_000, stage="reviewer", branch="quality")

        receipt = await extend_stage_budget(
            store,
            report_id="report-1",
            actor_id="actor-1",
            stage="reviewer",
            calls=32,
            branch="quality",
            branch_calls=24,
            note="third review pass needs ~7 more quality branch calls after two crashed attempts",
        )
        assert receipt["branch"] == "quality"
        assert receipt["previous_branch_calls"] == 16
        assert receipt["branch_calls"] == 24
        assert receipt["calls"] == 32

        budget = await store.load_budget(context())
        assert budget.stage_limits["reviewer"]["branches"]["quality"] == 24
        assert await budget.reserve(1_000, stage="reviewer", branch="quality")
        adjustment = (await store.read_control(context())).role_data[
            "budget_adjustments"
        ][-1]
        assert adjustment["branch"] == "quality"
        assert adjustment["previous_branch_calls"] == 16
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"branch": "typo", "branch_calls": 24}, "Unknown review branch"),
        ({"branch": "quality", "branch_calls": 16}, "only be extended"),
        ({"branch": "quality", "branch_calls": 40}, "stage call limit"),
        ({"branch_calls": 24}, "requires both branch"),
        ({}, "token or call limit"),
    ],
)
async def test_invalid_branch_extension_is_rejected(tmp_path, kwargs, match):
    engine, store = await _branch_store(tmp_path)
    try:
        with pytest.raises(ValueError, match=match):
            await extend_stage_budget(
                store,
                report_id="report-1",
                actor_id="actor-1",
                stage="reviewer",
                note="invalid attempt",
                **kwargs,
            )
        budget = await store.load_budget(context())
        assert budget.stage_limits["reviewer"]["branches"] == {
            "quality": 16,
            "compliance": 6,
        }
        assert not (await store.read_control(context())).role_data.get(
            "budget_adjustments"
        )
    finally:
        await engine.dispose()
