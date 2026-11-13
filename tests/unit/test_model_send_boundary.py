import json

import httpx
import pytest
from openai import AsyncOpenAI

from research_copilot.llm import MeteredChatModel
from research_copilot.llm.sdk import OpenAICompatibleChatModel
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.control import RunCancelled


@pytest.mark.parametrize(
    "scenario",
    ["completion_limit", "invalid_setting", "cancel_before_send", "budget_denied"],
)
async def test_local_validation_and_cancellation_precede_sent_reservation(scenario):
    bodies, audit = [], []
    cancelled = False

    async def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-42"},
            json={
                "id": "response-1",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    async def before_request():
        if cancelled:
            raise RunCancelled("cancelled")

    async def request_audit(event):
        nonlocal cancelled
        audit.append(event)
        if scenario == "cancel_before_send" and event["event"] == "started":
            cancelled = True

    async with AsyncOpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        inner = OpenAICompatibleChatModel(
            client=client,
            provider_id="offline",
            model_name="m",
            output_parameter="max_completion_tokens",
        )
        budget = TokenBudget(
            limit=1 if scenario == "budget_denied" else 10000, budget_id="b"
        )
        model = MeteredChatModel.wrap(
            inner,
            budget=budget,
            max_input_tokens=5000,
            max_output_tokens=100,
            output_parameter="max_completion_tokens",
            before_request=before_request,
            request_audit=request_audit,
        )
        if scenario == "budget_denied":
            from research_copilot.runtime.budget import BudgetExceeded

            with pytest.raises(BudgetExceeded):
                await model.ainvoke("hello")
            assert not bodies
            assert audit[-1]["event"] == "admission_rejected"
        elif scenario == "completion_limit":
            assert (await model.ainvoke("hello")).content == "ok"
            assert bodies[0]["max_completion_tokens"] == 100
            assert "max_tokens" not in bodies[0]
            assert budget.charged_tokens == 12
            assert audit[-1]["provider_request_id"] == "provider-request-42"
            assert (
                next(iter(budget.requests.values()))["details"]["provider_request_id"]
                == "provider-request-42"
            )
        else:
            with pytest.raises(
                ValueError if scenario == "invalid_setting" else RunCancelled
            ):
                await model.ainvoke(
                    "hello",
                    **({"unsupported": True} if scenario == "invalid_setting" else {}),
                )
            assert bodies == []
            assert budget.calls == budget.unknown_requests == budget.charged_tokens == 0
            assert budget.status()["reserved_tokens"] == 0
