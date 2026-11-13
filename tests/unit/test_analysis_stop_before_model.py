import pytest
from types import SimpleNamespace
from research_copilot.runtime.control import RunControl
from research_copilot.runtime.budget import BudgetExceeded
from research_copilot.subagents.middleware import RoleBusinessMiddleware


@pytest.mark.asyncio
async def test_exhausted_failed_analysis_stops_before_model_handler():
    state = SimpleNamespace(
        role_data={
            "scopes": {"d": {"task_brief": {"max_executions": 2}}},
            "executions": {
                str(i): {
                    "execution_id": str(i),
                    "dispatch_id": "d",
                    "status": "invalid_result",
                    "response": {"error": "bad metric"},
                }
                for i in range(2)
            },
        },
        dispatches={"d": {"inputs": {"plan": "p"}}},
    )

    class Services:
        async def scope(self, context, role):
            return SimpleNamespace(dispatch_id="d"), state

    request = SimpleNamespace(state={}, runtime=SimpleNamespace(context=None))

    async def forbidden(request):
        raise AssertionError("No paid model call allowed")

    with pytest.raises(BudgetExceeded, match="count"):
        await RoleBusinessMiddleware(
            "analysist", Services(), RunControl()
        ).awrap_model_call(request, forbidden)


def test_registered_execution_can_continue_to_analysis_submission():
    from research_copilot.sandbox.execution_policy import check_analysis_continuation

    scopes = {"d": {"task_brief": {"max_executions": 1}}}
    bindings = {"d": {"inputs": {"plan": "p"}}}
    check_analysis_continuation(
        "d",
        scopes,
        bindings,
        [{"dispatch_id": "d", "execution_id": "e", "status": "registered"}],
        20,
    )
