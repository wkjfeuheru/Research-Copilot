import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage

from research_copilot.runtime.business import ControlState


def test_supervisor_index_contains_references_not_attachment_body():
    from research_copilot.orchestration.context import supervisor_index

    state = ControlState(
        role_data={
            "evidence": {
                "E-000001": {
                    "kind": "attachment_original",
                    "content_hash": "a" * 64,
                    "raw": {"body": "PRIVATE LONG BODY", "title": "facts.txt"},
                    "source": {
                        "arguments": {"path": "/workspace/inputs/hash/facts.txt"}
                    },
                }
            }
        }
    )
    index = supervisor_index(state)
    assert index["next_action"] == "propose_plan"
    assert index["attachments"][0]["evidence_id"] == "E-000001"
    assert index["attachments"][0]["path"].endswith("facts.txt")
    assert "PRIVATE LONG BODY" not in json.dumps(index)


@pytest.mark.asyncio
async def test_supervisor_hides_directory_exploration_but_retains_business_tools():
    from research_copilot.orchestration.context import SupervisorContextMiddleware

    class Repository:
        async def read_control(self, context):
            return ControlState()

    class Request(SimpleNamespace):
        def override(self, **kwargs):
            return Request(**(vars(self) | kwargs))

    request = Request(
        runtime=SimpleNamespace(context=object()),
        system_message=SystemMessage(content="orchestrate"),
        tools=[
            {"name": name}
            for name in (
                "ls",
                "grep",
                "read_file",
                "task",
                "propose_plan",
                "confirm_plan",
            )
        ],
    )

    async def handler(updated):
        assert [t["name"] for t in updated.tools] == [
            "task",
            "propose_plan",
            "confirm_plan",
        ]
        assert '"next_action": "propose_plan"' in updated.system_message.content
        return "ok"

    assert (
        await SupervisorContextMiddleware(Repository()).awrap_model_call(
            request, handler
        )
        == "ok"
    )


@pytest.mark.asyncio
async def test_stage_rejection_reports_actual_reservation_and_remaining_without_charging():
    from research_copilot.runtime.budget import TokenBudget, BudgetExceeded

    b = TokenBudget(
        limit=2100000,
        budget_id="r5",
        stage_limits={"supervisor": {"calls": 10, "tokens": 200000}},
    )
    key = await b.reserve(150000)
    await b.settle(key, 124406)
    before = b.snapshot()
    with pytest.raises(BudgetExceeded) as error:
        await b.reserve(87720)
    assert error.value.details["requested_reservation"] == 87720
    assert error.value.details["stage_remaining_tokens"] == 75594
    assert error.value.details["known_actual_tokens"] == 124406
    assert b.snapshot() == before


def test_recovery_removes_directory_history_but_preserves_business_tool_pairs():
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from research_copilot.orchestration.context import routing_messages

    user = HumanMessage(content="original research request")
    mixed = AIMessage(
        content="",
        tool_calls=[
            {"name": "ls", "args": {"path": "/"}, "id": "directory"},
            {"name": "propose_plan", "args": {}, "id": "business"},
        ],
    )
    feedback = ToolMessage(
        content="plan requires objective", tool_call_id="business", name="propose_plan"
    )
    result = routing_messages(
        [
            user,
            mixed,
            ToolMessage(content="HUGE DIRECTORY", tool_call_id="directory", name="ls"),
            feedback,
        ]
    )
    assert result[0] == user and result[-1] == feedback
    assert len(result) == 3
    assert [c["id"] for c in result[1].tool_calls] == ["business"]
