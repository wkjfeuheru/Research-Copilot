import httpx

from research_copilot.llm.config import ModelsConfig, open_models


async def test_host_profile_and_sdk_wire_preserve_declared_image_capability(
    monkeypatch,
):
    import json
    from openai import AsyncOpenAI
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
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
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "reviewed"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    monkeypatch.setenv("IMAGE_TEST_KEY", "offline")
    monkeypatch.setattr(
        "research_copilot.llm.config.AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ),
    )
    roles = {
        role: {
            "provider_id": "p",
            "model": "m",
            "tool_calling": True,
            "context_tokens": 5000,
            "max_output_tokens": 100,
            "image_inputs": role == "reviewer",
        }
        for role in ("supervisor", "searcher", "analysist", "writer", "reviewer")
    }
    config = ModelsConfig.model_validate(
        {
            "providers": {
                "p": {
                    "base_url": "https://offline.invalid/v1",
                    "api_key_env": "IMAGE_TEST_KEY",
                }
            },
            "roles": roles,
        }
    )
    async with open_models(config) as models:
        reviewer = MeteredChatModel.wrap(
            models["reviewer"],
            budget=TokenBudget(limit=10000, budget_id="b"),
            max_input_tokens=5000,
            max_output_tokens=100,
        )
        assert reviewer.profile["image_inputs"] is True
        assert models["writer"].profile["image_inputs"] is False
        image = "data:image/png;base64,aGVsbG8="
        await reviewer.ainvoke(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Inspect frozen chart"},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ]
        )
        assert requests[0]["messages"][0]["content"][1]["image_url"]["url"] == image
