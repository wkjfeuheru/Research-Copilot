from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from tests.unit.test_orchestration import context


@pytest.fixture
async def store():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlSessionStore(engine)
    await store.setup()
    budget = TokenBudget(
        limit=10000,
        budget_id="budget-1",
        stage_limits={"searcher": {"calls": 10, "tokens": 10000}},
        prices={
            "m": {"version": "test", "input": "2", "cache_read": "1", "output": "3"}
        },
    )
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        budget.snapshot(),
    )
    try:
        yield store
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "previous,actual", [(None, 10), (100, 10), (None, 150), (100, 150), (100, 100)]
)
async def test_reconciliation_survives_sql_reload_with_atomic_totals_and_cost(
    store, previous, actual
):
    budget = await store.load_budget(context())
    key = await budget.reserve(100, stage="searcher")
    await budget.mark_sent(key)
    await budget.settle(
        key,
        previous,
        details={
            "model": "m",
            "input_tokens": 80,
            "output_tokens": 20,
            "usage_provisional": True,
        },
    )
    before = budget.snapshot()
    await budget.reconcile(
        key,
        actual,
        details={
            "source": "provider_bill",
            "actor_id": "actor-1",
            "note": "verified",
            "input_tokens": actual - 2,
            "output_tokens": 2,
            "cache_read_tokens": 0,
        },
    )
    restored = await store.load_budget(context())
    result = restored.snapshot()
    assert result == budget.snapshot()
    assert result["charged_tokens"] == result["known_actual_tokens"] == actual
    assert result["unknown_reserved_tokens"] == result["unknown_requests"] == 0
    assert result["stages"]["searcher"] == {
        "calls": 1,
        "known_actual_tokens": actual,
        "unknown_reserved_tokens": 0,
    }
    assert Decimal(result["estimated_cost"]) == Decimal((actual - 2) * 2 + 6) / 1000000
    assert result["requests"][key]["reconciliation"]["previous_usage"] == previous
    assert (
        result["requests"][key]["reconciliation"]["previous_details"]
        == before["requests"][key]["details"]
    )
    assert restored.status()["usage_verified"]
    await restored.reconcile(key, actual)
    assert restored.snapshot() == result
    with pytest.raises(ValueError, match="Conflicting"):
        await restored.reconcile(key, actual + 1)


async def test_reconciliation_does_not_reuse_provisional_cost_without_verified_breakdown(
    store,
):
    budget = await store.load_budget(context())
    key = await budget.reserve(100, stage="searcher")
    await budget.settle(
        key, 100, details={"model": "m", "input_tokens": 80, "output_tokens": 20}
    )
    await budget.reconcile(key, 10, details={"source": "provider_bill"})
    restored = await store.load_budget(context())
    assert "estimated_cost" not in restored.snapshot()["requests"][key]
    assert not restored.status()["cost_complete"]


async def test_stale_and_unaudited_refunds_cannot_overwrite_durable_usage(store):
    budget = await store.load_budget(context())
    key = await budget.reserve(100, stage="searcher")
    await budget.settle(key, None)
    stale = await store.load_budget(context())
    await budget.reconcile(key, 10, details={"source": "provider_bill"})
    with pytest.raises(ValueError, match="revision"):
        await stale.reconcile(key, 5)
    forged = await store.load_budget(context())
    forged.charged_tokens = 0
    with pytest.raises(ValueError, match="reconciliation"):
        await forged.reserve(1, stage="searcher")
    assert (await store.load_budget(context())).charged_tokens == 10
