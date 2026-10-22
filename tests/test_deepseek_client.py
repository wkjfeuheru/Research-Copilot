"""DeepSeek OpenAI 兼容适配器测试（使用 MockTransport，不访问网络）。"""

import json
from typing import Any

import httpx
import pytest
from openai import OpenAI, OpenAIError

from research_copilot.agents.planner import Planner
from research_copilot.config import Settings
from research_copilot.llm.client import ModelCallError, ModelRequest
from research_copilot.llm.deepseek import DeepSeekModelClient
from tests.planner_support import planner_request, ready_plan_payload


def _request() -> ModelRequest:
    return ModelRequest(
        system_prompt="system instructions",
        user_prompt="return a json plan",
        response_schema={"title": "PlannerOutput"},
        prompt_version="planner.v1",
        temperature=0.0,
    )


def _completion(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _client_with_handler(handler) -> DeepSeekModelClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    openai_client = OpenAI(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        http_client=http_client,
        max_retries=0,
    )
    return DeepSeekModelClient(
        api_key="test-key",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        max_tokens=2048,
        client=openai_client,
    )


def test_complete_sends_openai_compatible_json_request() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion('{"status":"ready"}'))

    client = _client_with_handler(handler)
    response = client.complete(_request())

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "deepseek-chat"
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "return a json plan"},
    ]
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["temperature"] == 0.0
    assert captured["body"]["max_tokens"] == 2048

    assert response.content == '{"status":"ready"}'
    assert response.model == "deepseek-chat"
    assert response.finish_reason == "stop"
    assert response.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_complete_returns_empty_content_when_no_choices() -> None:
    payload = _completion("")
    payload["choices"] = []

    client = _client_with_handler(lambda request: httpx.Response(200, json=payload))
    response = client.complete(_request())

    assert response.content == ""
    assert response.finish_reason is None


def test_http_error_is_wrapped_as_model_call_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "invalid api key",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
        )

    client = _client_with_handler(handler)

    with pytest.raises(ModelCallError) as exc_info:
        client.complete(_request())

    assert "DeepSeek API 调用失败" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OpenAIError)


def test_from_settings_requires_api_key() -> None:
    settings = Settings(environment="test", _env_file=None)

    with pytest.raises(ValueError, match="RC_RESEARCH_MODEL_API_KEY"):
        DeepSeekModelClient.from_settings(settings)


def test_from_settings_reads_model_and_supports_deepseek_chat_override() -> None:
    settings = Settings(
        environment="test",
        research_model="deepseek-chat",
        research_model_api_key="test-key",
        _env_file=None,
    )

    client = DeepSeekModelClient.from_settings(settings)

    assert client.model_name == "deepseek-chat"


def test_planner_accepts_deepseek_adapter_with_mock_transport() -> None:
    content = json.dumps(ready_plan_payload(), ensure_ascii=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content))

    client = _client_with_handler(handler)
    output = Planner(client).plan(planner_request())

    assert output.status.value == "ready"
    assert output.research_scope.object_type.value == "stock"
