import pytest
from langchain_core.messages import AIMessage, HumanMessage
from tests.unit.test_orchestration import ScriptModel
from research_copilot.llm import MeteredChatModel
from research_copilot.runtime.budget import TokenBudget, BudgetExceeded


def metered(message, budget=None):
    return MeteredChatModel.wrap(
        ScriptModel(script=[message]),
        budget=budget or TokenBudget(limit=100000, budget_id="p0"),
        max_input_tokens=50000,
        max_output_tokens=2048,
    )


def test_context_estimate_is_separate_from_budget_upper_bound():
    model = metered(AIMessage(content="ok"))
    messages = [HumanMessage(content="经营活动现金流量净额同比增长。" * 100)]
    assert model.get_num_tokens_from_messages(
        messages
    ) < model.reservation_input_tokens(messages)


@pytest.mark.asyncio
async def test_lower_context_estimate_does_not_relax_budget_reservation():
    model = metered(AIMessage(content="never"))
    messages = [HumanMessage(content="年报数据。" * 100)]
    model.budget.limit = (
        model.get_num_tokens_from_messages(messages) + model.max_output_tokens
    )
    with pytest.raises(BudgetExceeded):
        await model.ainvoke(messages)
    assert model.inner.position == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,text", [("stop", ""), ("content_filter", "filtered")])
async def test_invalid_summary_is_charged_once_and_rejected(finish, text):
    from research_copilot.llm.summary import checked_summarizer, IncompleteSummary

    model = metered(
        AIMessage(
            content=text,
            response_metadata={"finish_reason": finish},
            usage_metadata={
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
            },
        )
    )
    with pytest.raises(IncompleteSummary):
        await checked_summarizer(model).ainvoke([HumanMessage(content="source")])
    assert model.inner.position == 1
    assert model.budget.snapshot()["charged_tokens"] == 30
    assert not model.budget.snapshot()["pending"]


def metered_script(messages, budget=None):
    return MeteredChatModel.wrap(
        ScriptModel(script=messages),
        budget=budget or TokenBudget(limit=100000, budget_id="p0"),
        max_input_tokens=50000,
        max_output_tokens=2048,
    )


def _usage():
    return {
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
    }


@pytest.mark.asyncio
async def test_truncated_summary_retries_with_tighter_target_and_succeeds():
    from research_copilot.llm.summary import checked_summarizer

    model = metered_script(
        [
            AIMessage(
                content="cut off",
                response_metadata={"finish_reason": "length"},
                usage_metadata=_usage(),
            ),
            AIMessage(
                content="紧凑摘要",
                response_metadata={"finish_reason": "stop"},
                usage_metadata=_usage(),
            ),
        ]
    )
    assert (
        await checked_summarizer(model).ainvoke([HumanMessage(content="source")])
        == "紧凑摘要"
    )
    assert model.inner.position == 2
    assert model.budget.snapshot()["charged_tokens"] == 60


@pytest.mark.asyncio
async def test_truncated_summary_retry_exhaustion_is_rejected():
    from research_copilot.llm.summary import checked_summarizer, IncompleteSummary

    model = metered_script(
        [
            AIMessage(
                content="still too long",
                response_metadata={"finish_reason": "length"},
                usage_metadata=_usage(),
            )
        ]
    )
    with pytest.raises(IncompleteSummary, match="finish_reason='length'"):
        await checked_summarizer(model).ainvoke([HumanMessage(content="source")])
    assert model.inner.position == 2
    assert model.budget.snapshot()["charged_tokens"] == 60
    assert not model.budget.snapshot()["pending"]


@pytest.mark.asyncio
async def test_summary_uses_independent_model_and_records_usage_details():
    from research_copilot.llm.summary import checked_summarizer

    model = metered(
        AIMessage(
            content="goal; receipts; next",
            response_metadata={"finish_reason": "stop"},
            usage_metadata={
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "input_token_details": {"cache_read": 12},
                "output_token_details": {"reasoning": 2},
            },
        )
    )
    events = []

    async def audit(event):
        events.append(event)

    model.request_audit = audit
    assert (
        await checked_summarizer(model).ainvoke([HumanMessage(content="source")])
        == "goal; receipts; next"
    )
    assert events[-1]["input_tokens"] == 20
    assert events[-1]["cache_read_tokens"] == 12
    assert events[-1]["reasoning_tokens"] == 2
    assert events[-1]["elapsed_seconds"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_native_compaction_preserves_history_and_rejects_truncation(
    tmp_path, finish
):
    from langchain.agents import create_agent
    from deepagents.middleware import SummarizationMiddleware
    from langgraph.checkpoint.memory import InMemorySaver
    from research_copilot.llm.summary import checked_summarizer, IncompleteSummary
    from research_copilot.orchestration import filesystem_middleware
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.sandbox.backend import AnalysisSandboxBackend
    from research_copilot.workspace import build_backend, bundled_resources

    backend = AnalysisSandboxBackend(
        build_backend(tmp_path / "report", bundled_resources()), None
    )
    summary = metered(
        AIMessage(
            content="compact goal",
            response_metadata={"finish_reason": finish},
            # 真实供应商响应都带 usage；缺失会被预算账本视为未知请求，
            # 摘要重试需要显式授权。
            usage_metadata={
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
            },
        )
    )
    inner = ScriptModel(script=[AIMessage(content="finished")])
    graph = create_agent(
        inner,
        middleware=[
            RoleFileScopeMiddleware("analysist", allow_execute=True),
            filesystem_middleware(backend, execute=True),
            SummarizationMiddleware(
                model=inner,
                backend=backend,
                trigger=("messages", 3),
                keep=("messages", 1),
                summarizer=checked_summarizer(summary, backend=backend),
            ),
        ],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "p0"}}
    messages = [
        HumanMessage(content="original exact task A-H"),
        AIMessage(content="working"),
        HumanMessage(content="source fact"),
        AIMessage(content="more work"),
        HumanMessage(content="continue"),
    ]
    if finish == "length":
        with pytest.raises(IncompleteSummary):
            await graph.ainvoke({"messages": messages}, config)
        state = await graph.aget_state(config)
        assert state.values["messages"][0].content == "original exact task A-H"
        assert inner.position == 0
    else:
        result = await graph.ainvoke({"messages": messages}, config)
        assert any(
            "original exact task A-H" in f["content"] for f in result["files"].values()
        )
        assert inner.position == 1


@pytest.mark.asyncio
async def test_history_failure_prevents_summary_model_call():
    from types import SimpleNamespace
    from research_copilot.llm.summary import checked_summarizer, IncompleteSummary

    class BrokenBackend:
        async def awrite(self, path, text):
            return SimpleNamespace(error="storage unavailable")

        async def adownload_files(self, paths):
            return [SimpleNamespace(error="missing", content=None)]

    model = metered(AIMessage(content="unused"))
    with pytest.raises(IncompleteSummary, match="persistence"):
        await checked_summarizer(model, backend=BrokenBackend()).ainvoke(
            [HumanMessage(content="retain me")]
        )
    assert model.inner.position == 0
    assert model.budget.snapshot()["charged_tokens"] == 0
