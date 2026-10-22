"""Planner Agent 执行与错误语义测试。"""

import json
from typing import Any

import pytest

from research_copilot.agents.planner.agent import Planner
from research_copilot.agents.planner.errors import (
    PlannerErrorCode,
    PlannerModelError,
    PlannerOutputError,
)
from research_copilot.agents.planner.prompts import PLANNER_PROMPT_VERSION
from research_copilot.llm import ModelCallError
from tests.planner_fakes import FakeStructuredModelClient
from tests.planner_support import (
    company_comparison_plan_payload,
    industry_plan_payload,
    needs_clarification_payload,
    planner_request,
    ready_plan_payload,
    unsupported_payload,
)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _plan(
    content: str | Exception,
    *,
    request=None,
):
    client = FakeStructuredModelClient([content])
    output = Planner(client).plan(request or planner_request())
    return output, client


def test_ready_plan_is_returned_and_model_request_is_wired() -> None:
    output, client = _plan(_json(ready_plan_payload()))

    assert output.status.value == "ready"
    assert len(client.requests) == 1

    request = client.requests[0]
    assert request.prompt_version == PLANNER_PROMPT_VERSION
    assert request.response_schema["title"] == "PlannerOutput"
    assert "贵州茅台" in request.user_prompt
    assert "只输出一个 JSON 对象" in request.system_prompt


@pytest.mark.parametrize(
    ("payload_factory", "expected_status"),
    [
        (needs_clarification_payload, "needs_clarification"),
        (unsupported_payload, "unsupported"),
    ],
)
def test_clarification_and_unsupported_results_are_returned(
    payload_factory,
    expected_status: str,
) -> None:
    output, _ = _plan(_json(payload_factory()))

    assert output.status.value == expected_status


@pytest.mark.parametrize(
    ("payload_factory", "expected_object_type", "selected_skills"),
    [
        (ready_plan_payload, "stock", ["financial-analysis"]),
        (industry_plan_payload, "industry", ["industry-comparison"]),
        (
            company_comparison_plan_payload,
            "company_comparison",
            ["industry-comparison"],
        ),
    ],
)
def test_stock_industry_and_comparison_questions_form_plans(
    payload_factory,
    expected_object_type: str,
    selected_skills: list[str],
) -> None:
    request = planner_request(selected_skill_ids=selected_skills)
    output, client = _plan(_json(payload_factory()), request=request)

    assert output.status.value == "ready"
    assert output.research_scope.object_type.value == expected_object_type
    assert len(client.requests) == 1


def test_empty_model_response_is_rejected() -> None:
    client = FakeStructuredModelClient(["   "])

    with pytest.raises(PlannerOutputError) as exc_info:
        Planner(client).plan(planner_request())

    assert exc_info.value.code is PlannerErrorCode.EMPTY_MODEL_RESPONSE
    assert exc_info.value.raw_output == "   "
    assert len(client.requests) == 1


def test_invalid_json_is_rejected_without_repair() -> None:
    client = FakeStructuredModelClient(["不是 JSON"])

    with pytest.raises(PlannerOutputError) as exc_info:
        Planner(client).plan(planner_request())

    assert exc_info.value.code is PlannerErrorCode.INVALID_JSON
    assert exc_info.value.raw_output == "不是 JSON"
    assert len(client.requests) == 1


def test_fenced_json_is_not_silently_unwrapped() -> None:
    fenced = "```json\n" + _json(ready_plan_payload()) + "\n```"
    client = FakeStructuredModelClient([fenced])

    with pytest.raises(PlannerOutputError) as exc_info:
        Planner(client).plan(planner_request())

    assert exc_info.value.code is PlannerErrorCode.INVALID_JSON
    assert exc_info.value.raw_output == fenced


def test_missing_status_is_a_schema_failure() -> None:
    payload = ready_plan_payload()
    del payload["status"]
    client = FakeStructuredModelClient([_json(payload)])

    with pytest.raises(PlannerOutputError) as exc_info:
        Planner(client).plan(planner_request())

    error = exc_info.value
    assert error.code is PlannerErrorCode.SCHEMA_VALIDATION_FAILED
    assert error.validation_errors
    assert error.raw_output == _json(payload)
    assert json.dumps(error.validation_errors, ensure_ascii=False)


def test_unknown_field_is_a_schema_failure() -> None:
    payload = ready_plan_payload()
    payload["unexpected"] = "not allowed"
    client = FakeStructuredModelClient([_json(payload)])

    with pytest.raises(PlannerOutputError) as exc_info:
        Planner(client).plan(planner_request())

    assert exc_info.value.code is PlannerErrorCode.SCHEMA_VALIDATION_FAILED


def test_unknown_skill_is_a_business_validation_failure() -> None:
    payload = ready_plan_payload()
    payload["steps"][0]["skill_ids"] = ["not-in-input"]
    raw = _json(payload)
    client = FakeStructuredModelClient([raw])

    with pytest.raises(PlannerOutputError) as exc_info:
        Planner(client).plan(planner_request())

    assert exc_info.value.code is PlannerErrorCode.UNKNOWN_SKILL
    assert exc_info.value.raw_output == raw


def test_invalid_step_reference_is_a_business_validation_failure() -> None:
    payload = ready_plan_payload()
    payload["steps"][0]["depends_on"] = ["S9"]

    with pytest.raises(PlannerOutputError) as exc_info:
        Planner(_json_client(payload)).plan(planner_request())

    assert exc_info.value.code is PlannerErrorCode.INVALID_STEP_REFERENCE


def test_model_call_error_is_wrapped_and_not_retried() -> None:
    underlying = ModelCallError("provider timeout")
    client = FakeStructuredModelClient([underlying])

    with pytest.raises(PlannerModelError) as exc_info:
        Planner(client).plan(planner_request())

    error = exc_info.value
    assert error.code is PlannerErrorCode.MODEL_CALL_FAILED
    assert error.__cause__ is underlying
    assert len(client.requests) == 1


def test_unexpected_model_exception_is_wrapped_with_cause() -> None:
    underlying = RuntimeError("adapter bug")
    client = FakeStructuredModelClient([underlying])

    with pytest.raises(PlannerModelError) as exc_info:
        Planner(client).plan(planner_request())

    assert exc_info.value.code is PlannerErrorCode.MODEL_CALL_FAILED
    assert exc_info.value.__cause__ is underlying
    assert len(client.requests) == 1


def _json_client(payload: dict[str, Any]) -> FakeStructuredModelClient:
    return FakeStructuredModelClient([_json(payload)])
