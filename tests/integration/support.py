"""Offline model boundary and real native supervisor assembly for role integration."""

from typing import Any
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from tests.unit.test_orchestration import ScriptModel, call, review_graph


def tool_outputs(messages):
    import json
    from langchain_core.messages import ToolMessage

    outputs = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            try:
                value = json.loads(message.content)
                outputs[message.name] = (
                    value["data"]
                    if value.get("ok") is True and "data" in value
                    else value
                )
            except json.JSONDecodeError:
                outputs[message.name] = message.content
    return outputs


class RespondingModel(ScriptModel):
    respond: Any = Field(exclude=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.messages_seen[:] = messages
        response = self.respond(messages, self.position)
        self.position += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


async def approve_task(env, brief):
    """Establish explicit human-approved scope for a specialized offline fixture."""
    from research_copilot.runtime.business import ResearchPlan
    from tests.unit.test_orchestration import context

    _, business, store = env
    state = await store.read_control(context())
    plan = next(
        p["plan"]
        for p in state.plans
        if p["artifact"] == state.approved["plan"].model_dump()
    )
    tasks = [t for t in plan["tasks"] if t["role"] != brief.role] + [brief.model_dump()]
    ref = await business.propose_plan(
        context(), ResearchPlan.model_validate({**plan, "tasks": tasks})
    )
    await store.mutate_control(context(), lambda s: s.approved.update(plan=ref))


async def native_role(
    env, tmp_path, role, model, tools, schema, *, reviewer=None, saver=None, budget=None
):
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from research_copilot.orchestration import RoleBinding, build_supervisor
    from research_copilot.runtime.control import RunControl
    from research_copilot.workspace import bundled_resources
    from tests.unit.test_orchestration import context

    services, business, store = env
    bindings = {
        name: RoleBinding(
            ScriptModel(script=[AIMessage(content="unused")]), [], implemented=False
        )
        for name in ("searcher", "analysist", "writer")
    }
    if role in bindings:
        bindings[role] = RoleBinding(model, tools, response_schema=schema)
    from langchain_core.messages import ToolMessage

    import json

    state = await store.read_control(context())
    description = json.dumps(
        {
            "task_id": {
                "searcher": "collect",
                "analysist": "analysis",
                "writer": "write",
                "reviewer": "review",
            }[role],
            "role": role,
            "objective": "scoped task",
            "as_of": state.role_data.get("research_as_of", "2025-04-30"),
        }
    )
    parent = RespondingModel(
        script=[],
        respond=lambda messages, step: (
            AIMessage(content="done")
            if any(
                isinstance(message, ToolMessage) and message.name == "task"
                for message in messages
            )
            else call("task", {"subagent_type": role, "description": description})
        ),
    )
    return build_supervisor(
        model=parent,
        roles=bindings,
        review_graph=reviewer or review_graph(),
        report_id="report-1",
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
        checkpointer=saver or InMemorySaver(),
        business=business,
        control=RunControl(),
        budget=budget or await store.load_budget(context()),
    )
