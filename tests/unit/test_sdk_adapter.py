import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import AsyncOpenAI


@pytest.mark.parametrize("followup", [False, True])
async def test_tool_images_use_user_content_after_parallel_tool_receipts(followup):
    from copy import deepcopy
    from research_copilot.llm.sdk import OpenAICompatibleChatModel

    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "inspect"},
            tool_calls=[
                {"id": name, "name": "read_snapshot_chart", "args": {}}
                for name in ("chart-a", "chart-b")
            ],
        ),
        ToolMessage(
            content=[{"type": "text", "text": "hash-verified chart-a"}, picture],
            tool_call_id="chart-a",
        ),
        ToolMessage(content=[picture], tool_call_id="chart-b"),
    ]
    if followup:
        messages.append(HumanMessage(content="Continue review"))
    original = deepcopy(messages)
    bodies = []

    async def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        wire = body["messages"]
        assert [m["role"] for m in wire] == [
            "assistant",
            "tool",
            "tool",
            "user",
        ] + (["user"] if followup else [])
        assert wire[0]["reasoning_content"] == "inspect"
        assert [m["tool_call_id"] for m in wire[1:3]] == ["chart-a", "chart-b"]
        assert "hash-verified chart-a" in str(wire[1]["content"])
        assert all(picture not in m["content"] for m in wire[1:3])
        assert [b for b in wire[3]["content"] if b["type"] == "image_url"] == [
            picture,
            picture,
        ]
        assert "chart-a" in str(wire[3]) and "chart-b" in str(wire[3])
        return httpx.Response(
            200,
            json={
                "id": "image-test",
                "object": "chat.completion",
                "created": 1,
                "model": "vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "reviewed"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 25,
                    "completion_tokens": 1,
                    "total_tokens": 26,
                },
            },
        )

    async with AsyncOpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = OpenAICompatibleChatModel(
            client=client,
            provider_id="test",
            model_name="vision",
            reasoning_field="reasoning_content",
        )
        prepared = model.prepare_request(messages, max_tokens=32)
        assert await model.ainvoke(messages, max_tokens=32)
        assert bodies[0] == prepared
        assert model.global_input_bound(messages, []) >= len(
            json.dumps(
                {"messages": prepared["messages"], "extra_body": {}, "tools": []},
                ensure_ascii=False,
            ).encode()
        ) + 32 * (len(prepared["messages"]) + 1)
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding", ["explicit", "supervisor", "searcher", "analysist", "writer", "reviewer"]
)
async def test_thinking_provider_structured_agent_uses_configured_auto_choice(binding):
    """Reproduce the first Searcher request through the real ToolStrategy path."""
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy
    from pydantic import BaseModel
    from research_copilot.llm.sdk import OpenAICompatibleChatModel
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    class Result(BaseModel):
        summary: str

    bodies = []

    async def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["tool_choice"] != "auto":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Thinking mode does not support this tool_choice",
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 1,
                "model": "thinking-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "reasoning",
                            "tool_calls": [
                                {
                                    "id": "result",
                                    "type": "function",
                                    "function": {
                                        "name": "Result",
                                        "arguments": '{"summary":"validated"}',
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
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = OpenAICompatibleChatModel(
            client=client,
            provider_id="thinking-vendor",
            model_name="thinking-model",
            required_tool_choice="auto",
            reasoning_field="reasoning_content",
        )
        if binding != "explicit":
            from pathlib import Path
            from research_copilot.llm.config import load_models_config

            role = load_models_config(
                Path(__file__).resolve().parents[2] / "config/models.example.toml"
            ).roles[binding]
            model = OpenAICompatibleChatModel(
                client=client,
                provider_id=role.provider_id,
                model_name=role.model,
                required_tool_choice=role.required_tool_choice,
                reasoning_field=role.reasoning_field,
                extra_body=role.extra_body,
            )
        metered = MeteredChatModel.wrap(
            model,
            budget=TokenBudget(limit=50000, budget_id="test"),
            max_input_tokens=32000,
            max_output_tokens=1000,
        )
        agent = create_agent(
            metered, response_format=ToolStrategy(Result, handle_errors=False)
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Return a result"}]}
        )
        assert result["structured_response"] == Result(summary="validated")
        assert len(bodies) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,token_parameter,reasoning_field",
    [
        ("vendor-a", "max_tokens", "reasoning_content"),
        ("vendor-b", "max_completion_tokens", None),
    ],
)
async def test_openai_sdk_protocol_preserves_tools_reasoning_usage_and_provider_isolation(
    provider, token_parameter, reasoning_field
):
    from research_copilot.llm.sdk import OpenAICompatibleChatModel

    requests = []

    async def respond(request):
        requests.append(request)
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tool-1",
                    "type": "function",
                    "function": {"name": "get_report_status", "arguments": "{}"},
                }
            ],
        }
        if reasoning_field:
            message[reasoning_field] = "private reasoning"
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "object": "chat.completion",
                "created": 1,
                "model": "custom-model",
                "choices": [
                    {"index": 0, "message": message, "finish_reason": "tool_calls"}
                ],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 10,
                    "total_tokens": 40,
                },
            },
            headers={"x-request-id": "request-1"},
        )

    async with AsyncOpenAI(
        api_key=f"{provider}-secret",
        base_url=f"https://{provider}.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = OpenAICompatibleChatModel(
            client=client,
            provider_id=provider,
            model_name="custom-model",
            output_parameter=token_parameter,
            reasoning_field=reasoning_field,
        )
        previous = AIMessage(
            content="",
            tool_calls=[{"name": "get_report_status", "args": {}, "id": "previous"}],
            additional_kwargs={"reasoning_content": "old thinking"},
        )
        response = await model.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "get_report_status",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        ).ainvoke(
            [
                HumanMessage(content="research"),
                previous,
                ToolMessage(content="ready", tool_call_id="previous"),
            ],
            max_tokens=100,
        )
        body = json.loads(requests[0].content)
        assert body[token_parameter] == 100
        assert body["messages"][-1]["tool_call_id"] == "previous"
        assert body["model"] == "custom-model"
        assert requests[0].url.host == f"{provider}.invalid"
        assert requests[0].headers["authorization"] == f"Bearer {provider}-secret"
        assert response.tool_calls[0]["id"] == "tool-1"
        assert response.usage_metadata["total_tokens"] == 40
        assert response.response_metadata["request_id"] == "request-1"
        if reasoning_field:
            assert body["messages"][1][reasoning_field] == "old thinking"
            assert response.additional_kwargs[reasoning_field] == "private reasoning"
        else:
            assert "reasoning_content" not in body["messages"][1]
        assert "secret" not in repr(model)
        assert "secret" not in json.dumps(model.model_dump(), default=str)


@pytest.mark.asyncio
async def test_sdk_errors_are_not_retried_and_usage_remains_unknown():
    from research_copilot.llm.sdk import OpenAICompatibleChatModel
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget
    from openai import RateLimitError

    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            429, json={"error": {"message": "limited", "type": "rate_limit"}}
        )

    async with AsyncOpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = OpenAICompatibleChatModel(
            client=client, provider_id="offline", model_name="custom"
        )
        budget = TokenBudget(limit=10000, budget_id="b")
        metered = MeteredChatModel.wrap(
            model, budget=budget, max_input_tokens=5000, max_output_tokens=100
        )
        with pytest.raises(RateLimitError):
            await metered.ainvoke("research")
        assert len(requests) == 1
        assert budget.snapshot()["unknown_requests"] == 1
        assert budget.snapshot()["charged_tokens"] > 0


@pytest.mark.asyncio
async def test_role_model_configuration_uses_separate_clients_and_closes_them(
    monkeypatch,
):
    from research_copilot.llm.config import ModelsConfig, open_models
    from pydantic import ValidationError

    monkeypatch.setenv("current_A_KEY", "secret-a")
    monkeypatch.setenv("current_B_KEY", "secret-b")
    roles = {
        name: {
            "provider_id": "a" if name != "writer" else "b",
            "model": "custom-" + name,
            "context_tokens": 32000,
            "max_output_tokens": 1000,
            "tool_calling": True,
        }
        for name in ("supervisor", "searcher", "analysist", "writer", "reviewer")
    }
    data = {
        "providers": {
            "a": {"base_url": "https://a.invalid/v1", "api_key_env": "current_A_KEY"},
            "b": {"base_url": "https://b.invalid/v1", "api_key_env": "current_B_KEY"},
        },
        "roles": roles,
    }
    config = ModelsConfig.model_validate(data)
    data["roles"]["searcher"]["required_tool_choice"] = "auto"
    data["summarizer"] = {
        **roles["supervisor"],
        "model": "summary-only",
        "tool_calling": False,
        "max_output_tokens": 2048,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    config = ModelsConfig.model_validate(data)
    async with open_models(config) as models:
        first, second = models["supervisor"].client, models["writer"].client
        assert first is not second
        assert first.max_retries == 0
        assert models["writer"].model_name == "custom-writer"
        assert models["writer"].profile["max_input_tokens"] == 32000
        assert models["searcher"].required_tool_choice == "auto"
        assert models["writer"].required_tool_choice == "required"
        assert models["summarizer"].model_name == "summary-only"
        assert models["summarizer"].extra_body == {"thinking": {"type": "disabled"}}
        assert models["summarizer"].profile["max_output_tokens"] == 2048
    assert first.is_closed() and second.is_closed()
    data["roles"]["writer"]["tool_calling"] = False
    with pytest.raises(ValidationError, match="tool"):
        ModelsConfig.model_validate(data)


@pytest.mark.asyncio
async def test_provider_extensions_cannot_override_the_metered_request_contract():
    from research_copilot.llm.sdk import OpenAICompatibleChatModel
    from pydantic import ValidationError

    async with AsyncOpenAI(api_key="test", max_retries=0) as client:
        with pytest.raises(ValidationError, match="override"):
            OpenAICompatibleChatModel(
                client=client,
                provider_id="offline",
                model_name="custom",
                extra_body={"max_tokens": 1000000},
            )
