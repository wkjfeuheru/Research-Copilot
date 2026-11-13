import pytest
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import tool

from research_copilot.subagents.contracts import SearchResult
from research_copilot.subagents.middleware import RoleBusinessMiddleware
from research_copilot.runtime.control import RunControl
from tests.integration.test_searcher import dispatch
from tests.unit.test_orchestration import ScriptModel, call, context


@pytest.mark.asyncio
async def test_uncooperative_model_stops_with_valid_partial_and_persistent_counter(
    role_env,
):
    services, business, store = role_env
    scope = await dispatch(role_env)
    await store.mutate_control(
        context(),
        lambda s: s.role_data["scopes"][scope.dispatch_id].update(no_progress_calls=2),
    )

    @tool
    async def web_search(query: str):
        """Offline source that must not be called during wrap-up."""
        raise AssertionError("Source must not run")

    model = ScriptModel(
        script=[
            call("web_search", {"query": "repeat"}, "a"),
            call("web_search", {"query": "repeat"}, "b"),
        ]
    )
    control = RunControl()

    def graph():
        return create_agent(
            model,
            tools=[web_search],
            response_format=ToolStrategy(SearchResult),
            middleware=[
                RoleBusinessMiddleware("searcher", services, control, ["web_search"])
            ],
        )

    async with control.dispatch(scope):
        result = await graph().ainvoke(
            {"messages": [("user", "collect")]}, context=context()
        )
        assert result["structured_response"].status == "partial"
        assert result["structured_response"].gaps
        parsed = await services.map_result(
            context(), scope, result["structured_response"].model_dump()
        )
        await business.accept_result(context(), scope, parsed)
        assert parsed.status == "needs_input"
        assert model.position == 2
        # Reconstructing middleware/graph cannot reset the persisted limit.
        again = await graph().ainvoke(
            {"messages": [("user", "collect")]}, context=context()
        )
        assert again["structured_response"].status == "partial"
        assert model.position == 2


@pytest.mark.asyncio
async def test_truncated_tool_submission_reaches_persisted_partial(role_env):
    from langchain_core.messages import AIMessage

    services, business, store = role_env
    scope = await dispatch(role_env)
    truncated = AIMessage(
        content="现在提交事实清单。",
        invalid_tool_calls=[
            {"name": "SearchResult", "args": '{"facts":[', "id": "cut", "error": None}
        ],
        response_metadata={"finish_reason": "length"},
    )
    model = ScriptModel(script=[truncated, truncated])
    control = RunControl()
    graph = create_agent(
        model,
        response_format=ToolStrategy(SearchResult),
        middleware=[RoleBusinessMiddleware("searcher", services, control)],
    )
    async with control.dispatch(scope):
        output = await graph.ainvoke(
            {"messages": [("user", "collect")]}, context=context()
        )
        result = output["structured_response"]
        assert model.position == 2 and result.status == "partial"
        parsed = await services.map_result(context(), scope, result.model_dump())
        await business.accept_result(context(), scope, parsed)
    state = await store.read_control(context())
    assert state.results[scope.dispatch_id]["status"] == "needs_input"
