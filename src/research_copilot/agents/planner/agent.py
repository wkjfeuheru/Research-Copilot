"""Planner Agent 执行入口。

职责：组装提示词、调用注入的模型客户端、严格解析 JSON、校验契约并返回
``PlannerOutput``。模型失败、空响应、非法 JSON、Schema 错误和业务校验错误都会
显式抛出，不会生成默认计划、不会重试、不会降级为 mock。
"""

import json

from pydantic import ValidationError

from research_copilot.agents.planner.contracts import (
    PlannerInput,
    PlannerOutput,
    planner_output_schema,
)
from research_copilot.agents.planner.errors import (
    PlannerErrorCode,
    PlannerModelError,
    PlannerOutputError,
)
from research_copilot.agents.planner.prompts import (
    PLANNER_PROMPT_VERSION,
    build_planner_prompt,
)
from research_copilot.agents.planner.validation import validate_planner_output
from research_copilot.llm import ModelRequest, StructuredModelClient


class Planner:
    """依赖注入模型客户端的 Planner Agent。"""

    def __init__(
        self,
        model_client: StructuredModelClient,
        *,
        prompt_version: str = PLANNER_PROMPT_VERSION,
    ) -> None:
        self._model_client = model_client
        self._prompt_version = prompt_version

    def plan(self, request: PlannerInput) -> PlannerOutput:
        """生成并校验研究计划。

        异常：
            - ``PlannerModelError``：模型客户端调用失败。
            - ``PlannerOutputError``：空响应、非法 JSON、Schema 或业务校验失败。
        """

        system_prompt, user_prompt = build_planner_prompt(request)
        model_request = ModelRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=planner_output_schema(),
            prompt_version=self._prompt_version,
        )

        try:
            response = self._model_client.complete(model_request)
        except Exception as exc:  # 模型边界：保留原始异常链，不伪装成功
            raise PlannerModelError(
                PlannerErrorCode.MODEL_CALL_FAILED,
                f"模型调用失败：{type(exc).__name__}: {exc}",
            ) from exc

        raw_content = response.content
        content = raw_content.strip()
        if not content:
            raise PlannerOutputError(
                PlannerErrorCode.EMPTY_MODEL_RESPONSE,
                "模型返回了空内容",
                raw_output=raw_content,
            )

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PlannerOutputError(
                PlannerErrorCode.INVALID_JSON,
                f"模型输出不是合法 JSON：{exc.msg}"
                f"（第 {exc.lineno} 行，第 {exc.colno} 列）",
                raw_output=raw_content,
            ) from exc

        try:
            output = PlannerOutput.model_validate(payload)
        except ValidationError as exc:
            validation_errors = json.loads(
                exc.json(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            )
            raise PlannerOutputError(
                PlannerErrorCode.SCHEMA_VALIDATION_FAILED,
                f"模型输出不符合 Planner Schema：{len(validation_errors)} 处错误",
                raw_output=raw_content,
                validation_errors=validation_errors,
            ) from exc

        try:
            validate_planner_output(request, output)
        except PlannerOutputError as exc:
            raise PlannerOutputError(
                exc.code,
                exc.message,
                raw_output=raw_content,
                validation_errors=exc.validation_errors,
            ) from exc

        return output


__all__ = ["Planner"]
