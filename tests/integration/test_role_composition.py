"""Production composition and SDK structured-output contract, no live requests."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.control import RunControl
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.workspace import bundled_resources
from tests.unit.test_orchestration import ScriptModel, call, context, config


@pytest.mark.asyncio
async def test_factory_defaults_to_real_business_roles(role_env, tmp_path):
    services, business, store = role_env
    models = {
        name: ScriptModel(script=[AIMessage(content="unused")])
        for name in ("supervisor", "searcher", "analysist", "writer", "reviewer")
    }
    models["supervisor"].script = [
        call(
            "task",
            {
                "subagent_type": "searcher",
                "description": '{"task_id":"collect","role":"searcher","objective":"collect","as_of":"2025-04-30"}',
            },
        ),
        AIMessage(content="done"),
    ]
    models["searcher"].script = [
        call(
            "SearchResult",
            {
                "task_id": "collect",
                "status": "blocked",
                "summary": "Source not configured",
                "gaps": [
                    {
                        "description": "Sina authorization absent",
                        "impact": "Cannot acquire requested data",
                        "suggested_role": "supervisor",
                    }
                ],
            },
        )
    ]
    for model in models.values():
        for message in model.script:
            message.usage_metadata = {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            }
    factory = SupervisorFactory(
        models=models,
        store=store,
        business=business,
        checkpointer=InMemorySaver(),
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
    )
    agent = await factory(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        RunControl(),
    )
    result = await agent.ainvoke(
        {"messages": [("user", "collect")]}, config(), context=context()
    )
    assert models["searcher"].position == 1
    assert any(
        isinstance(m, ToolMessage) and "needs_input" in m.content
        for m in result["messages"]
    )
    assert all(
        models[role].position == 0 for role in ("analysist", "writer", "reviewer")
    )
    state = await store.read_control(context())
    # Admission timeouts are host policy and must not invalidate historical checkpoints.
    assert "queue_seconds" not in state.role_data["runtime_manifest"]["sandbox"]
    requests = [
        item
        for item in state.role_data.get("model_audit", [])
        if item["event"] == "settled"
    ]
    assert sorted(item["role"] for item in requests) == [
        "searcher",
        "supervisor",
        "supervisor",
    ]


@pytest.mark.asyncio
async def test_structured_tool_strategy_uses_valid_openai_tool_choice():
    import json
    import httpx
    from openai import AsyncOpenAI
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy
    from research_copilot.llm.sdk import OpenAICompatibleChatModel
    from research_copilot.subagents.contracts import SearchResult

    requests = []

    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["tool_choice"] == "required"
        return httpx.Response(
            200,
            json={
                "id": "r",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "result",
                                    "type": "function",
                                    "function": {
                                        "name": "SearchResult",
                                        "arguments": json.dumps(
                                            {
                                                "task_id": "search",
                                                "status": "completed",
                                                "summary": "No additional facts required",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )

    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = OpenAICompatibleChatModel(
            client=client, provider_id="offline", model_name="m"
        )
        agent = create_agent(
            model=model, response_format=ToolStrategy(SearchResult, handle_errors=False)
        )
        result = await agent.ainvoke({"messages": [("user", "search")]})
        assert result["structured_response"].status == "completed"
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_status_graph_cannot_replace_active_report_sandbox(
    role_env, tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock
    from research_copilot.subagents import analysist

    services, business, store = role_env
    constructed = []
    original = analysist.build_analysis_tools

    def capture(*args, **kwargs):
        constructed.append(kwargs.get("execution"))
        return original(*args, **kwargs)

    monkeypatch.setattr(analysist, "build_analysis_tools", capture)
    factory = SupervisorFactory(
        models={
            role: ScriptModel(script=[AIMessage(content="unused")])
            for role in ("supervisor", "searcher", "analysist", "writer", "reviewer")
        },
        store=store,
        business=business,
        checkpointer=InMemorySaver(),
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
    )
    record = SessionRecord(
        "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
    )
    active = await factory(record, RunControl())
    inspection = await factory(record, RunControl())
    assert active is not inspection and all(constructed)
    assert constructed[0] is not constructed[1]
    constructed[0].runner.cancel_report = AsyncMock()
    await factory.cancel_report("report-1")
    constructed[0].runner.cancel_report.assert_awaited_once_with("report-1")


@pytest.fixture(autouse=True)
async def native_factory_identity(role_env):
    from research_copilot.runtime.autonomy import freeze_scope

    def native(state):
        state.role_data["workflow_version"] = "products-v2"
        freeze_scope(
            state,
            "report-1",
            {
                "report_type": "company_report",
                "objective": "research",
                "as_of": "2025-04-30",
            },
        )

    await role_env[2].mutate_control(context(), native)
