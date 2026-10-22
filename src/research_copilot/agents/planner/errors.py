"""Planner 的可追踪错误类型。"""

from enum import Enum
from typing import Any


class PlannerErrorCode(str, Enum):
    """错误分类，便于测试、日志和后续 HTTP 层映射。"""

    MODEL_CALL_FAILED = "model_call_failed"
    EMPTY_MODEL_RESPONSE = "empty_model_response"
    INVALID_JSON = "invalid_json"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    REQUIRED_CONTENT_MISSING = "required_content_missing"
    INVALID_STATUS_CONTENT = "invalid_status_content"
    DUPLICATE_IDENTIFIER = "duplicate_identifier"
    INVALID_STEP_REFERENCE = "invalid_step_reference"
    INVALID_DATA_REFERENCE = "invalid_data_reference"
    INVALID_STEP_DEPENDENCY = "invalid_step_dependency"
    UNKNOWN_SKILL = "unknown_skill"


class PlannerError(Exception):
    """Planner 错误基类。"""

    def __init__(
        self,
        code: PlannerErrorCode,
        message: str,
        *,
        raw_output: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.raw_output = raw_output


class PlannerModelError(PlannerError):
    """模型调用阶段失败，未产生可校验的规划结果。"""


class PlannerOutputError(PlannerError):
    """模型返回内容不满足 JSON、Schema 或 Planner 业务约束。"""

    def __init__(
        self,
        code: PlannerErrorCode,
        message: str,
        *,
        raw_output: str | None = None,
        validation_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(code, message, raw_output=raw_output)
        self.validation_errors = validation_errors or []


__all__ = [
    "PlannerError",
    "PlannerErrorCode",
    "PlannerModelError",
    "PlannerOutputError",
]
