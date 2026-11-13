import pytest
from langchain_core.messages import AIMessage

from tests.unit.test_orchestration import ScriptModel


@pytest.mark.asyncio
async def test_direct_summary_call_and_bound_tool_call_share_the_same_ledger():
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    budget = TokenBudget(limit=10000, budget_id="budget-1")
    inner = ScriptModel(
        script=[
            AIMessage(
                content="summary",
                usage_metadata={
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "total_tokens": 25,
                },
            )
        ]
    )
    model = MeteredChatModel.wrap(
        inner, budget=budget, max_input_tokens=5000, max_output_tokens=100
    )
    await model.ainvoke("summarize")
    await model.bind_tools([]).ainvoke("research")
    assert budget.snapshot()["charged_tokens"] == 50
    assert budget.snapshot()["pending"] == {}
    assert model.profile["max_input_tokens"] == 5000


@pytest.mark.asyncio
async def test_over_capacity_request_raises_context_overflow_for_recovery():
    """容量超限必须抛 ContextOverflowError，摘要中间件才能压缩重试而不是整轮失败。"""
    from langchain_core.exceptions import ContextOverflowError
    from langchain_core.messages import HumanMessage

    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    model = MeteredChatModel.wrap(
        ScriptModel(script=[]),
        budget=TokenBudget(limit=10**6, budget_id="b"),
        max_input_tokens=1000,
        max_output_tokens=100,
    )
    with pytest.raises(ContextOverflowError):
        await model.ainvoke([HumanMessage(content="研究" * 2000)])


def test_reservation_estimate_is_calibrated_not_raw_byte_count():
    # 中文 1 字约 0.6 token，而非 3 个 UTF-8 字节；字节法会把预留高估约 3 倍
    from langchain_core.messages import HumanMessage
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    model = MeteredChatModel.wrap(
        ScriptModel(script=[]),
        budget=TokenBudget(limit=10**9, budget_id="b"),
        max_input_tokens=10**6,
        max_output_tokens=1,
    )
    message = HumanMessage(
        content="研究采集预算" * 500
    )  # 3500 个中文字符，UTF-8 10500 字节
    estimate = model.reservation_input_tokens([message])
    payload_bytes = len(message.content.encode("utf-8"))
    assert estimate < payload_bytes  # 修复前：字节数直接当预留，必然失败
    assert estimate >= 3500 * 0.6  # 不得低于真实 token 量级下界


@pytest.mark.asyncio
async def test_cjk_history_reserves_calibrated_estimate_not_bytes():
    # 复现线上场景：中文历史消息在字节预留法下无法预留，校准法可以
    from langchain_core.messages import HumanMessage
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    budget = TokenBudget(
        limit=10**9,
        budget_id="b",
        stage_limits={"searcher": {"calls": 5, "tokens": 5000}},
    )
    inner = ScriptModel(
        script=[
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 900,
                    "output_tokens": 10,
                    "total_tokens": 900,
                },
            )
        ]
    )
    model = MeteredChatModel.wrap(
        inner,
        budget=budget,
        max_input_tokens=100000,
        max_output_tokens=200,
        budget_stage="searcher",
    )
    await model.ainvoke(
        [HumanMessage(content="研究采集预算" * 500)]
    )  # 字节预留 ~10.7k > 5000
    assert inner.position == 1


@pytest.mark.asyncio
async def test_total_mismatch_falls_back_to_component_usage_as_provisional():
    # provider 带 reasoning 时 total != input+output 很常见；
    # 修复前整单判未知、按预留值扣款，修复后按分项和扣款并标记待对账
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    budget = TokenBudget(limit=10000, budget_id="b")
    inner = ScriptModel(
        script=[
            AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 150,
                },
            )
        ]
    )
    model = MeteredChatModel.wrap(
        inner, budget=budget, max_input_tokens=5000, max_output_tokens=100
    )
    await model.ainvoke("research")
    state = budget.snapshot()
    assert state["charged_tokens"] == 120
    assert state["unknown_requests"] == 0
    request = next(iter(state["requests"].values()))
    assert request["details"]["usage_provisional"] is True


@pytest.mark.asyncio
async def test_budget_exhaustion_and_context_overflow_prevent_model_requests():
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import BudgetExceeded, TokenBudget

    inner = ScriptModel(script=[AIMessage(content="should not execute")])
    model = MeteredChatModel.wrap(
        inner,
        budget=TokenBudget(limit=10, budget_id="b"),
        max_input_tokens=5000,
        max_output_tokens=100,
    )
    with pytest.raises(BudgetExceeded):
        await model.ainvoke("research")
    assert inner.position == 0
    model = MeteredChatModel.wrap(
        inner,
        budget=TokenBudget(limit=10000, budget_id="b"),
        max_input_tokens=100,
        max_output_tokens=50,
    )
    # 容量超限改用 ContextOverflowError，便于摘要中间件压缩后重试
    from langchain_core.exceptions import ContextOverflowError

    with pytest.raises(ContextOverflowError, match="context"):
        await model.ainvoke("research" * 100)
    assert inner.position == 0


@pytest.mark.asyncio
async def test_global_admission_rejects_ambiguous_token_estimate_before_sdk_call():
    import httpx
    from openai import AsyncOpenAI
    from research_copilot.llm import MeteredChatModel
    from research_copilot.llm.sdk import OpenAICompatibleChatModel
    from research_copilot.runtime.budget import TokenBudget, BudgetExceeded

    requests = []

    async def deny(req):
        requests.append(req)
        raise AssertionError("globally unaffordable request reached provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(deny)) as client:
        sdk = OpenAICompatibleChatModel(
            client=AsyncOpenAI(api_key="offline", http_client=client, max_retries=0),
            model_name="offline",
            provider_id="test",
        )
        budget = TokenBudget(limit=2100000, budget_id="r", charged_tokens=1990598)
        model = MeteredChatModel.wrap(
            sdk, budget=budget, max_input_tokens=131072, max_output_tokens=8192
        )
        with pytest.raises(BudgetExceeded):
            await model.ainvoke("17,482.76元 " * 14000)
        assert requests == [] and budget.calls == 0


@pytest.mark.asyncio
async def test_global_headroom_is_shared_across_pending_requests_after_reload():
    from research_copilot.runtime.budget import TokenBudget, BudgetExceeded

    b = TokenBudget(limit=100, budget_id="b")
    reservation = await b.reserve(20, global_bound=85)
    restored = TokenBudget(**b.snapshot())
    assert restored.status()["remaining_tokens"] == 15
    with pytest.raises(BudgetExceeded):
        await restored.reserve(10, global_bound=16)
    await restored.settle(reservation, None)
    assert restored.charged_tokens == 85
    await restored.reconcile(reservation, 18)
    assert restored.charged_tokens == 18 and restored.status()["remaining_tokens"] == 82
