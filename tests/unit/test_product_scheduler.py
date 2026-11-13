from types import SimpleNamespace
import json
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain.agents.middleware.types import ModelResponse
from research_copilot.runtime.business import ControlState
from research_copilot.runtime.scheduler import ProductWorkflowMiddleware
from research_copilot.runtime.workflow import WorkflowBlocked, decide_next
from research_copilot.runtime.budget import TokenBudget, BudgetExceeded
from research_copilot.runtime.control import RunControl
from tests.unit.test_product_workflow import state


@pytest.mark.asyncio
async def test_checkpointed_stage_budget_failure_keeps_reason_and_never_retries():
    from research_copilot.runtime.conversation import public_error

    s = state()
    action = decide_next(s, "r")
    details = {
        "stage": "searcher",
        "requested_reservation": 61842,
        "stage_remaining_tokens": 40373,
        "unknown_reserved_tokens": 96588,
    }
    mw, req = setup(
        s,
        messages=[
            ToolMessage(
                tool_call_id=action["id"],
                content=json.dumps(
                    {"error": "stage_budget_exhausted", "details": details}
                ),
            )
        ],
    )
    before = mw.budget.snapshot()

    async def forbidden(request):
        raise AssertionError("Must not retry a paid dispatch")

    with pytest.raises(BudgetExceeded) as caught:
        await mw.awrap_model_call(req, forbidden)
    assert caught.value.details == details
    message = public_error(caught.value)
    assert "采集" in message and "61,842" in message and "40,373" in message
    assert "96,588" in message
    assert mw.budget.snapshot() == before


def setup(s, budget=None, messages=()):
    class Repo:
        async def read_control(self, context):
            return s

    class Request(SimpleNamespace):
        def override(self, **kwargs):
            return Request(**(vars(self) | kwargs))

    req = Request(
        runtime=SimpleNamespace(context=SimpleNamespace(report_id="r")),
        messages=list(messages),
        tools=[{"name": "propose_plan"}],
        system_message=None,
    )
    mw = ProductWorkflowMiddleware(
        SimpleNamespace(repository=Repo()),
        RunControl(),
        budget or TokenBudget(limit=100, budget_id="b"),
    )
    return mw, req


@pytest.mark.asyncio
async def test_planner_cannot_smuggle_dispatch_outside_exposed_tools():
    mw, req = setup(ControlState())

    async def handler(request):
        return ModelResponse(
            result=[
                AIMessage(
                    content="", tool_calls=[{"name": "task", "args": {}, "id": "x"}]
                )
            ]
        )

    with pytest.raises(WorkflowBlocked):
        await mw.awrap_model_call(req, handler)


@pytest.mark.asyncio
async def test_budget_stops_dispatch_before_model():
    mw, req = setup(
        state(), TokenBudget(limit=100, budget_id="b", call_limit=1, calls=1)
    )

    async def forbidden(request):
        raise AssertionError("No supervisor model")

    with pytest.raises(BudgetExceeded):
        await mw.awrap_model_call(req, forbidden)


@pytest.mark.asyncio
async def test_rejected_gate_does_not_retry_or_call_model():
    s = state()
    s.approved.clear()
    a = decide_next(s, "r")
    mw, req = setup(s, messages=[ToolMessage(content="rejected", tool_call_id=a["id"])])

    async def forbidden(request):
        raise AssertionError("No supervisor model")

    with pytest.raises(WorkflowBlocked):
        await mw.awrap_model_call(req, forbidden)


@pytest.mark.asyncio
async def test_planning_question_is_returned_to_user_instead_of_workflow_blocked():
    mw, req = setup(ControlState())

    async def ask_period(request):
        return ModelResponse(result=[AIMessage(content="请补充要比较的两个季度。")])

    response = await mw.awrap_model_call(req, ask_period)
    assert response.result[0].content == "请补充要比较的两个季度。"
    assert not response.result[0].tool_calls
    assert response.result[0].additional_kwargs["awaiting_user_input"] is True


@pytest.mark.asyncio
async def test_invalid_role_result_in_checkpoint_explains_missing_submission():
    action = decide_next(state(), "r")
    mw, req = setup(
        state(),
        messages=[
            ToolMessage(
                tool_call_id=action["id"],
                content=json.dumps({"error": "invalid_subagent_result"}),
            )
        ],
    )
    with pytest.raises(WorkflowBlocked, match="未提交有效的结构化结果"):
        await mw.awrap_model_call(req, None)
