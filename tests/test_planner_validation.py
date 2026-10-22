"""Planner 业务校验测试。"""

from typing import Any

import pytest

from research_copilot.agents.planner.contracts import PlannerInput, PlannerOutput
from research_copilot.agents.planner.errors import PlannerErrorCode, PlannerOutputError
from research_copilot.agents.planner.validation import validate_planner_output
from tests.planner_support import (
    needs_clarification_payload,
    planner_request,
    ready_plan_payload,
    unsupported_payload,
)


def _validate(
    payload: dict[str, Any],
    request: PlannerInput | None = None,
) -> None:
    output = PlannerOutput.model_validate(payload)
    validate_planner_output(request or planner_request(), output)


def _expect_error(
    payload: dict[str, Any],
    code: PlannerErrorCode,
    request: PlannerInput | None = None,
) -> PlannerOutputError:
    with pytest.raises(PlannerOutputError) as exc_info:
        _validate(payload, request)
    assert exc_info.value.code is code
    return exc_info.value


def test_ready_plan_passes() -> None:
    _validate(ready_plan_payload())


def test_needs_clarification_passes() -> None:
    _validate(needs_clarification_payload())


def test_unsupported_passes() -> None:
    _validate(unsupported_payload())


def test_duplicate_step_ids_are_rejected() -> None:
    payload = ready_plan_payload()
    payload["steps"].append(payload["steps"][0])

    _expect_error(payload, PlannerErrorCode.DUPLICATE_IDENTIFIER)


def test_duplicate_requirement_ids_are_rejected() -> None:
    payload = ready_plan_payload()
    payload["data_requirements"].append(payload["data_requirements"][0])

    _expect_error(payload, PlannerErrorCode.DUPLICATE_IDENTIFIER)


def test_unknown_step_dependency_is_rejected() -> None:
    payload = ready_plan_payload()
    payload["steps"][0]["depends_on"] = ["S9"]

    _expect_error(payload, PlannerErrorCode.INVALID_STEP_REFERENCE)


def test_self_step_dependency_is_rejected() -> None:
    payload = ready_plan_payload()
    payload["steps"][0]["depends_on"] = ["S1"]

    _expect_error(payload, PlannerErrorCode.INVALID_STEP_REFERENCE)


def test_step_dependency_cycle_is_rejected() -> None:
    payload = ready_plan_payload()
    payload["steps"][0]["depends_on"] = ["S4"]

    _expect_error(payload, PlannerErrorCode.INVALID_STEP_DEPENDENCY)


def test_unknown_data_reference_is_rejected() -> None:
    payload = ready_plan_payload()
    payload["steps"][0]["data_requirement_ids"] = ["D9"]

    _expect_error(payload, PlannerErrorCode.INVALID_DATA_REFERENCE)


def test_unknown_skill_is_rejected() -> None:
    payload = ready_plan_payload()
    payload["steps"][0]["skill_ids"] = ["not-in-input"]

    _expect_error(payload, PlannerErrorCode.UNKNOWN_SKILL)


def test_ready_requires_objective() -> None:
    payload = ready_plan_payload()
    payload["objective"] = None

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_ready_requires_analysis_dimensions() -> None:
    payload = ready_plan_payload()
    payload["objective"]["analysis_dimensions"] = []

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_ready_requires_data_requirements() -> None:
    payload = ready_plan_payload()
    payload["data_requirements"] = []
    for step in payload["steps"]:
        step["data_requirement_ids"] = []

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_ready_requires_steps() -> None:
    payload = ready_plan_payload()
    payload["steps"] = []

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_ready_requires_all_four_capabilities() -> None:
    payload = ready_plan_payload()
    payload["steps"] = payload["steps"][:3]

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_ready_requires_every_data_requirement_to_be_referenced() -> None:
    payload = ready_plan_payload()
    payload["data_requirements"].append(
        {
            "requirement_id": "D2",
            "description": "补充一项没有被步骤引用的数据需求。",
            "material_types": ["market_data"],
            "purpose": "测试未引用数据需求。",
        }
    )

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_ready_rejects_clarification_questions() -> None:
    payload = ready_plan_payload()
    payload["clarification_questions"] = ["还需要问一个问题吗？"]

    _expect_error(payload, PlannerErrorCode.INVALID_STATUS_CONTENT)


def test_ready_rejects_unsupported_reason() -> None:
    payload = ready_plan_payload()
    payload["unsupported_reason"] = "不应出现。"

    _expect_error(payload, PlannerErrorCode.INVALID_STATUS_CONTENT)


def test_needs_clarification_requires_question() -> None:
    payload = needs_clarification_payload()
    payload["clarification_questions"] = []

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_needs_clarification_rejects_unsupported_reason() -> None:
    payload = needs_clarification_payload()
    payload["unsupported_reason"] = "不应出现。"

    _expect_error(payload, PlannerErrorCode.INVALID_STATUS_CONTENT)


def test_unsupported_requires_reason() -> None:
    payload = unsupported_payload()
    payload["unsupported_reason"] = None

    _expect_error(payload, PlannerErrorCode.REQUIRED_CONTENT_MISSING)


def test_unsupported_rejects_steps() -> None:
    payload = unsupported_payload()
    payload["steps"] = [
        {
            "step_id": "S1",
            "intent": "不应生成步骤。",
            "capabilities": ["analysis"],
            "depends_on": [],
            "data_requirement_ids": [],
            "skill_ids": [],
        }
    ]

    _expect_error(payload, PlannerErrorCode.INVALID_STATUS_CONTENT)


def test_unsupported_rejects_data_requirements() -> None:
    payload = unsupported_payload()
    payload["data_requirements"] = [
        {
            "requirement_id": "D1",
            "description": "不应生成数据需求。",
            "material_types": ["other"],
            "purpose": "测试不支持状态。",
        }
    ]

    _expect_error(payload, PlannerErrorCode.INVALID_STATUS_CONTENT)


def test_unsupported_rejects_clarification_questions() -> None:
    payload = unsupported_payload()
    payload["clarification_questions"] = ["不应生成待澄清问题。"]

    _expect_error(payload, PlannerErrorCode.INVALID_STATUS_CONTENT)
