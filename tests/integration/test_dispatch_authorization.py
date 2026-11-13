import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.orchestration.middleware import DispatchMiddleware
from research_copilot.runtime.control import RunControl
from research_copilot.subagents.contracts import BusinessInputError
from tests.unit.test_orchestration import context


@pytest.mark.parametrize(
    "raw",
    [
        {
            "role": "searcher",
            "status": "completed",
            "summary": "legacy bypass",
            "references": [],
            "gaps": [],
        },
        {
            "status": "completed",
            "summary": "missing identity",
            "artifact_refs": [],
            "gaps": [],
        },
    ],
)
async def test_business_role_cannot_fall_back_to_legacy_result(role_env, raw):
    _, business, store = role_env
    brief = TaskBrief(
        task_id="collect", role="searcher", objective="collect", as_of="2025-04-30"
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context()),
        tool_call={
            "name": "task",
            "id": "legacy-result",
            "args": {
                "subagent_type": "searcher",
                "description": brief.model_dump_json(),
            },
        },
    )
    request.override = lambda **kwargs: SimpleNamespace(**{**vars(request), **kwargs})

    async def handler(_):
        return ToolMessage(
            tool_call_id="legacy-result", name="task", content=json.dumps(raw)
        )

    result = await DispatchMiddleware(
        business, RunControl(), "report-1"
    ).awrap_tool_call(request, handler)
    assert result.status == "error"
    assert json.loads(result.content)["error"] == "invalid_subagent_result"
    assert not (await store.read_control(context())).results


@pytest.mark.parametrize(
    "task_id,duplicate", [("unapproved", False), ("collect", True)]
)
async def test_dispatch_requires_unique_frozen_plan_task(role_env, task_id, duplicate):
    services, business, store = role_env
    if duplicate:

        def corrupt(state):
            state.plans[-1]["plan"]["tasks"].append(state.plans[-1]["plan"]["tasks"][0])

        await store.mutate_control(context(), corrupt)
    scope = Dispatch("unapproved", "searcher", "/workspace/tasks/unapproved/")
    await business.authorize_dispatch(context(), scope)
    brief = TaskBrief(
        task_id=task_id, role="searcher", objective="collect", as_of="2025-04-30"
    )
    with pytest.raises(BusinessInputError, match="unique.*approved"):
        await services.prepare_dispatch(context(), scope, brief.model_dump_json())
    assert scope.dispatch_id not in (await store.read_control(context())).role_data.get(
        "scopes", {}
    )
