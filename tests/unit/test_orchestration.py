"""Offline acceptance of the real Deep Agents assembly; no model/network calls."""

import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt
from pydantic import Field


class ScriptModel(BaseChatModel):
    model_name: str = "offline-script"
    profile: dict = Field(
        default_factory=lambda: {"max_input_tokens": 100000, "max_output_tokens": 1000}
    )
    script: list[AIMessage]
    tools_seen: list = Field(default_factory=list, exclude=True)
    messages_seen: list = Field(default_factory=list, exclude=True)
    position: int = 0

    @property
    def _llm_type(self):
        return "offline-script"

    def bind_tools(self, tools, **kwargs):
        self.tools_seen[:] = tools
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.messages_seen[:] = messages
        message = self.script[min(self.position, len(self.script) - 1)]
        self.position += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name, args, ident="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": ident}])


def review_graph():
    async def review(state):
        interrupt(
            {
                "kind": "reviewer_deep_review",
                "snapshot": {"version": "draft-1"},
                "allowed_decisions": ["approve", "request_changes", "reject"],
            }
        )
        return {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        {
                            "status": "passed",
                            "summary": "checked",
                            "issues": [],
                            "comments": [],
                            "reviewed": {
                                "artifact_id": "draft-1",
                                "version": "1",
                                "content_hash": "a" * 64,
                            },
                        }
                    )
                )
            ]
        }

    graph = StateGraph(MessagesState)
    graph.add_node("review", review)
    graph.add_edge(START, "review")
    graph.add_edge("review", END)
    return graph.compile()


class Business:
    """External business store stand-in; records authorization at the boundary."""

    def __init__(self):
        self.dispatches = []
        self.confirmations = []

    async def authorize_dispatch(self, context, dispatch):
        self.dispatches.append(dispatch)

    async def accept_result(self, context, dispatch, result):
        return result

    async def get_status(self, context):
        return {"report_id": context.report_id, "approved": False}

    async def confirm(self, context, action, artifact, idempotency_key):
        self.confirmations.append((context, action, artifact, idempotency_key))
        return {
            "receipt_id": "receipt-1",
            "action": action,
            "artifact": artifact.model_dump(),
        }


def assemble(
    tmp_path,
    model,
    *,
    business=None,
    checkpointer=None,
    roles=None,
    budget=None,
    autonomous_workflow=False,
    report_id="report-1",
):
    from research_copilot.orchestration import RoleBinding, build_supervisor
    from research_copilot.runtime.control import RunControl
    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.workspace import bundled_resources
    from shutil import copytree

    resources = tmp_path / "resources"
    copytree(bundled_resources(), resources, dirs_exist_ok=True)
    roles = roles or {
        name: RoleBinding(
            model=ScriptModel(
                script=[
                    AIMessage(
                        content=json.dumps(
                            {
                                "role": name,
                                "status": "unavailable",
                                "summary": "not implemented",
                                "references": [],
                                "gaps": ["role implementation outside scope"],
                            }
                        )
                    )
                ]
            ),
            tools=[],
        )
        for name in ("searcher", "analysist", "writer")
    }
    return build_supervisor(
        model=model,
        roles=roles,
        review_graph=review_graph(),
        report_id=report_id,
        workspace_root=tmp_path / "workspace",
        resources_root=resources,
        checkpointer=checkpointer or InMemorySaver(),
        business=business or Business(),
        control=RunControl(),
        budget=budget or TokenBudget(limit=1000000, budget_id="budget-1"),
        autonomous_workflow=autonomous_workflow,
    )


def context():
    from research_copilot.contracts import RunContext

    return RunContext(
        actor_id="actor-1",
        report_id="report-1",
        run_id="run-1",
        budget_id="budget-1",
        policy_version="policy-1",
    )


def config():
    return {"configurable": {"thread_id": "v2:report-1"}, "recursion_limit": 100}


@pytest.mark.asyncio
async def test_supervisor_receives_host_time_before_first_model_call(tmp_path):
    import re
    from datetime import datetime

    model = ScriptModel(script=[AIMessage(content="ready")])
    agent = assemble(tmp_path, model, autonomous_workflow=True)
    await agent.ainvoke({"messages": [("user", "研究最新季度")]}, config(), context=context())
    system = "\n".join(str(message.content) for message in model.messages_seen if message.type == "system")
    match = re.search(r"运行时间（Asia/Shanghai）：([^。\s]+)", system)
    assert match is not None, "The model must receive the host clock, not infer today's date"
    assert datetime.fromisoformat(match[1]).utcoffset().total_seconds() == 28800


@pytest.mark.asyncio
async def test_only_four_roles_and_no_shell_are_advertised(tmp_path):
    # A forgotten profile registration would silently add general-purpose.
    model = ScriptModel(script=[AIMessage(content="ready")])
    agent = assemble(tmp_path, model)
    await agent.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    tools = {tool.name: tool for tool in model.tools_seen}
    assert "execute" not in tools
    assert {
        "task",
        "get_report_status",
        "confirm_plan",
        "confirm_analysis",
        "confirm_rework",
        "signoff_report",
    } <= tools.keys()
    schema = tools["task"].description
    for name in ("searcher", "analysist", "writer", "reviewer"):
        assert name in schema
    # The framework's generic usage paragraph mentions general-purpose even when disabled.
    available = schema.split(
        "Available agent types and the tools they have access to:"
    )[1].split("Specify subagent_type")[0]
    assert "general-purpose" not in available


@pytest.mark.asyncio
async def test_dispatch_is_authorized_and_unavailable_role_does_not_claim_success(
    tmp_path,
):
    business = Business()
    model = ScriptModel(
        script=[
            call("task", {"subagent_type": "searcher", "description": "collect"}),
            AIMessage(content="role unavailable"),
        ]
    )
    result = await assemble(tmp_path, model, business=business).ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert len(business.dispatches) == 1
    dispatch = business.dispatches[0]
    assert dispatch.role == "searcher"
    assert dispatch.write_prefix.startswith("/workspace/tasks/")
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert json.loads(messages[-1].content)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_confirmation_is_paused_and_bound_to_exact_artifact(tmp_path):
    business = Business()
    artifact = {"artifact_id": "plan-1", "version": "1", "content_hash": "a" * 64}
    model = ScriptModel(
        script=[
            call("confirm_plan", {"artifact": artifact}),
            AIMessage(content="confirmed"),
        ]
    )
    agent = assemble(tmp_path, model, business=business)
    first = await agent.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert first["__interrupt__"]
    assert business.confirmations == []
    await agent.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config(),
        context=context(),
    )
    assert business.confirmations[0][1] == "confirm_plan"
    assert business.confirmations[0][2].content_hash == "a" * 64
    assert business.confirmations[0][0].actor_id == "actor-1"


@pytest.mark.asyncio
async def test_reviewer_interrupt_propagates_and_resumes_from_parent(tmp_path):
    saver = InMemorySaver()
    model = ScriptModel(
        script=[
            call(
                "task", {"subagent_type": "reviewer", "description": "review draft-1"}
            ),
            AIMessage(content="review complete"),
        ]
    )
    agent = assemble(tmp_path, model, checkpointer=saver)
    first = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    pending = first["__interrupt__"][0]
    assert pending.value["kind"] == "reviewer_deep_review"
    # A fresh assembly shares the durable graph checkpoint, never restarts the child alone.
    agent = assemble(tmp_path, model, checkpointer=saver)
    result = await agent.ainvoke(
        Command(resume={pending.id: {"decision": "approve"}}),
        config(),
        context=context(),
    )
    assert not result.get("__interrupt__")
    assert result["messages"][-1].content == "review complete"


def test_tampered_resources_are_rejected_before_assembly(tmp_path):
    from research_copilot.workspace import bundled_resources, validate_resources
    from shutil import copytree

    copytree(bundled_resources(), tmp_path, dirs_exist_ok=True)
    (tmp_path / "skills/writer/reports/SKILL.md").write_text(
        "changed", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="hash"):
        validate_resources(tmp_path)
