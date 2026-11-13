import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from tests.unit.test_orchestration import ScriptModel, context, config, review_graph


@pytest.mark.asyncio
async def test_native_supervisor_receives_routing_index_without_filesystem_tools(
    role_env, tmp_path
):
    from research_copilot.orchestration import RoleBinding, build_supervisor
    from research_copilot.runtime.control import RunControl
    from research_copilot.workspace import bundled_resources

    _, business, store = role_env
    parent = ScriptModel(script=[AIMessage(content="ready")])
    roles = {
        name: RoleBinding(ScriptModel(script=[AIMessage(content="unused")]), [])
        for name in ("searcher", "analysist", "writer")
    }
    agent = build_supervisor(
        model=parent,
        roles=roles,
        review_graph=review_graph(),
        report_id="report-1",
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
        checkpointer=InMemorySaver(),
        business=business,
        control=RunControl(),
        budget=await store.load_budget(context()),
    )
    await agent.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    names = {
        t.name if hasattr(t, "name") else t["function"]["name"]
        for t in parent.tools_seen
    }
    assert {"task", "propose_plan", "confirm_plan"} <= names
    assert not names & {"ls", "grep", "read_file", "write_file", "glob", "edit_file"}
    assert "Host routing index" in str(parent.messages_seen[0].content)
