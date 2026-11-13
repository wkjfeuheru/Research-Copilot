import asyncio
import pytest
from research_copilot.runtime.budget import TokenBudget, BudgetExceeded


@pytest.mark.asyncio
async def test_unknown_usage_is_not_actual_and_duplicate_settlement_is_idempotent():
    budget = TokenBudget(limit=1000, budget_id="b")
    first = await budget.reserve(100)
    await budget.settle(first, 40)
    await budget.settle(first, 40)
    second = await budget.reserve(100)
    await budget.settle(second, None)
    state = budget.snapshot()
    assert state["known_actual_tokens"] == 40
    assert state["unknown_reserved_tokens"] == 100
    assert state["charged_tokens"] == 140
    restored = TokenBudget(**state)
    await restored.settle(second, None)
    assert restored.snapshot() == state


@pytest.mark.asyncio
async def test_reconcile_corrects_unknown_and_provisional_usage():
    # 对账：provider 账单到位后校正未知占额与暂定用量，释放虚占预算
    budget = TokenBudget(
        limit=1000,
        budget_id="b",
        stage_limits={"searcher": {"calls": 5, "tokens": 500}},
    )
    key = await budget.reserve(300, stage="searcher")
    await budget.settle(key, None)
    assert budget.snapshot()["unknown_reserved_tokens"] == 300
    await budget.reconcile(key, 120, details={"source": "provider_bill"})
    state = budget.snapshot()
    assert state["unknown_requests"] == 0
    assert state["unknown_reserved_tokens"] == 0
    assert state["known_actual_tokens"] == 120
    assert state["charged_tokens"] == 120
    request = state["requests"][key]
    assert request["reconciled"] is True
    assert request["details"]["usage_provisional"] is False
    # 幂等：相同值重复对账不变化；不同值冲突
    await budget.reconcile(key, 120)
    assert budget.snapshot()["charged_tokens"] == 120
    with pytest.raises(ValueError, match="Conflict"):
        await budget.reconcile(key, 999)
    # 暂定用量按差额校正
    second = await budget.reserve(100, stage="searcher")
    await budget.settle(second, 120)
    await budget.reconcile(second, 100)
    state = budget.snapshot()
    assert state["known_actual_tokens"] == 220
    assert state["charged_tokens"] == 220
    # 未结算请求不能对账
    third = await budget.reserve(50, stage="searcher")
    with pytest.raises(ValueError, match="settled"):
        await budget.reconcile(third, 10)


@pytest.mark.asyncio
async def test_parallel_stage_and_global_reservations_cannot_bypass_call_limit():
    budget = TokenBudget(
        limit=1000,
        budget_id="b",
        call_limit=2,
        stage_limits={"reviewer": {"calls": 1, "tokens": 500}},
    )
    outcomes = await asyncio.gather(
        budget.reserve(100, stage="reviewer"),
        budget.reserve(100, stage="reviewer"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, BudgetExceeded) for r in outcomes) == 1
    reservation = next(r for r in outcomes if isinstance(r, str))
    await budget.settle(reservation, 20)
    with pytest.raises(BudgetExceeded):
        await budget.reserve(100, stage="reviewer")
    assert budget.snapshot()["stages"]["reviewer"]["known_actual_tokens"] == 20


@pytest.mark.asyncio
async def test_cache_discount_changes_cost_but_not_actual_tokens():
    budget = TokenBudget(
        limit=1000,
        budget_id="b",
        prices={
            "m": {"version": "test", "input": "2", "cache_read": "0.2", "output": "3"}
        },
    )
    key = await budget.reserve(200, stage="writer")
    await budget.settle(
        key,
        120,
        details={
            "model": "m",
            "input_tokens": 100,
            "cache_read_tokens": 80,
            "output_tokens": 20,
        },
    )
    snapshot = budget.snapshot()
    assert snapshot["known_actual_tokens"] == 120
    assert snapshot["estimated_cost"] == "0.000116"


@pytest.mark.asyncio
async def test_release_is_allowed_only_before_send():
    budget = TokenBudget(limit=1000, budget_id="b")
    key = await budget.reserve(100)
    await budget.mark_sent(key)
    with pytest.raises(ValueError, match="sent"):
        await budget.release_unsent(key)
    other = await budget.reserve(100)
    await budget.release_unsent(other)
    assert other not in budget.snapshot()["pending"]


@pytest.mark.asyncio
async def test_metered_model_assigns_calls_and_audit_failure_does_not_charge_unsent():
    from langchain_core.messages import AIMessage
    from tests.unit.test_orchestration import ScriptModel
    from research_copilot.llm import MeteredChatModel

    budget = TokenBudget(
        limit=10000,
        budget_id="b",
        stage_limits={"writer": {"calls": 2, "tokens": 10000}},
    )
    model = MeteredChatModel.wrap(
        ScriptModel(
            script=[
                AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "total_tokens": 25,
                    },
                )
            ]
        ),
        budget=budget,
        max_input_tokens=5000,
        max_output_tokens=100,
        budget_stage="writer",
    )
    await model.ainvoke("write")
    assert budget.snapshot()["stages"]["writer"]["known_actual_tokens"] == 25

    async def failing_audit(event):
        raise OSError("audit unavailable")

    model.request_audit = failing_audit
    with pytest.raises(OSError):
        await model.ainvoke("write again")
    assert budget.calls == 1
    assert budget.unknown_requests == 0


@pytest.mark.asyncio
async def test_stale_persistence_failure_poisoned_instance_cannot_overwrite_another_request():
    durable = TokenBudget(limit=1000, budget_id="shared").snapshot()

    async def persist(snapshot):
        nonlocal durable
        if snapshot["revision"] != durable["revision"] + 1:
            raise ValueError("stale revision")
        durable = snapshot

    a, b = (
        TokenBudget(**durable, persist=persist),
        TokenBudget(**durable, persist=persist),
    )
    first = await a.reserve(50)
    with pytest.raises(ValueError, match="stale"):
        await b.reserve(20)
    with pytest.raises(RuntimeError, match="reload"):
        await b.reserve(20)
    assert durable["pending"] == {first: 50}


@pytest.mark.asyncio
async def test_cancelled_settlement_finishes_durable_write_before_cancellation_propagates():
    entered, finish = asyncio.Event(), asyncio.Event()
    persisted = []
    budget = TokenBudget(limit=1000, budget_id="cancel")
    key = await budget.reserve(100)
    await budget.mark_sent(key)

    async def persist(snapshot):
        entered.set()
        await finish.wait()
        persisted.append(snapshot)

    budget._persist = persist
    settling = asyncio.create_task(budget.settle(key, 20))
    await entered.wait()
    settling.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await settling
    assert persisted and persisted[-1]["known_actual_tokens"] == 20
    assert not persisted[-1]["pending"]


def test_remaining_stage_calls_supports_wrap_up_and_unlimited_stages():
    budget = TokenBudget(
        limit=1000,
        budget_id="b",
        stage_limits={"analysist": {"calls": 3, "tokens": 900}},
    )
    assert budget.remaining_stage_calls("analysist") == 3
    assert budget.remaining_stage_calls("supervisor") is None


@pytest.mark.asyncio
async def test_stage_call_consumption_drives_wrap_up_threshold():
    budget = TokenBudget(
        limit=1000,
        budget_id="b",
        stage_limits={"analysist": {"calls": 2, "tokens": 900}},
    )
    first = await budget.reserve(10, stage="analysist")
    await budget.settle(first, None)
    assert budget.remaining_stage_calls("analysist") == 1
