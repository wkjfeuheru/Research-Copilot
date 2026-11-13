import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command
from tests.unit.test_orchestration import ScriptModel, assemble, call, config, context


@pytest.mark.asyncio
async def test_native_todos_and_clarification_checkpoint(tmp_path):
    model = ScriptModel(
        script=[
            call(
                "write_todos",
                {"todos": [{"content": "Research", "status": "in_progress"}]},
                "todos",
            ),
            call("ask_user", {"question": "Which company?"}, "question"),
            AIMessage(content="Received answer"),
        ]
    )
    agent = assemble(tmp_path, model, autonomous_workflow=True)
    result = await agent.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert result["todos"][0]["status"] == "in_progress"
    pending = result["__interrupt__"][0]
    assert pending.value["kind"] == "clarification"
    result = await agent.ainvoke(
        Command(resume={pending.id: {"answer": "Company A"}}),
        config(),
        context=context(),
    )
    assert not result.get("__interrupt__")
    names = {t.name for t in model.tools_seen}
    assert {
        "write_todos",
        "task",
        "ask_user",
        "set_report_scope",
        "signoff_report",
    } <= names
    assert not names & {
        "propose_plan",
        "confirm_plan",
        "confirm_analysis",
        "confirm_rework",
    }
    assert model.position == 3


@pytest.mark.asyncio
async def test_parent_selects_additional_collection_from_role_feedback(tmp_path):
    import json
    from research_copilot.orchestration import RoleBinding
    from tests.integration.support import RespondingModel, tool_outputs
    from tests.unit.test_orchestration import Business

    business = Business()

    def choose(messages, step):
        result = tool_outputs(messages).get("task")
        if result and result["status"] == "completed":
            return call(
                "ask_user", {"question": "Confirm the missing business context?"}, "ask"
            )
        return call(
            "task",
            {
                "subagent_type": "searcher",
                "description": "initial evidence"
                if result is None
                else "fill the reported evidence gap",
            },
            "collect-" + str(step),
        )

    parent = RespondingModel(script=[], respond=choose)
    roles = {
        role: RoleBinding(ScriptModel(script=[AIMessage(content="unused")]), [])
        for role in ("searcher", "analysist", "writer")
    }
    roles["searcher"] = RoleBinding(
        ScriptModel(
            script=[
                AIMessage(
                    content=json.dumps(
                        {
                            "role": "searcher",
                            "status": status,
                            "summary": "evidence result",
                            "references": [],
                            "gaps": ["missing evidence"]
                            if status == "needs_input"
                            else [],
                        }
                    )
                )
                for status in ("needs_input", "completed")
            ]
        ),
        [],
    )
    graph = assemble(
        tmp_path, parent, business=business, roles=roles, autonomous_workflow=True
    )
    result = await graph.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert result["__interrupt__"][0].value["kind"] == "clarification"
    assert [dispatch.role for dispatch in business.dispatches] == [
        "searcher",
        "searcher",
    ]
    assert parent.position == 3
