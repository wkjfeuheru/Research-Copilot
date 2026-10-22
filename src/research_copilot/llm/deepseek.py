"""DeepSeek OpenAI 兼容模型适配器。

DeepSeek API 支持 OpenAI SDK 调用方式：
- base_url: https://api.deepseek.com
- JSON Output: response_format={"type": "json_object"}
- 提示词必须要求 JSON，并设置合理 max_tokens。

来源：
- https://api-docs.deepseek.com/
- https://api-docs.deepseek.com/guides/json_mode/
- https://api-docs.deepseek.com/api/create-chat-completion

适配器只负责调用和返回原始文本；JSON 解析、Schema 与业务校验仍由 Planner 负责。
"""

from __future__ import annotations

from typing import Any

from openai import OpenAI, OpenAIError

from research_copilot.config import Settings
from research_copilot.llm.client import (
    ModelCallError,
    ModelRequest,
    ModelResponse,
)


class DeepSeekModelClient:
    """通过 OpenAI 兼容接口调用 DeepSeek Chat Completions。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 60.0,
        max_tokens: int = 8192,
        client: OpenAI | None = None,
    ) -> None:
        normalized_key = api_key.strip()
        normalized_model = model.strip()
        normalized_base_url = base_url.strip().rstrip("/")

        if not normalized_key:
            raise ValueError("DeepSeek API key 不能为空")
        if not normalized_model:
            raise ValueError("DeepSeek 模型名不能为空")
        if not normalized_base_url.startswith(("http://", "https://")):
            raise ValueError("DeepSeek base_url 必须以 http:// 或 https:// 开头")
        if timeout_seconds <= 0:
            raise ValueError("DeepSeek timeout_seconds 必须大于 0")
        if max_tokens < 1:
            raise ValueError("DeepSeek max_tokens 必须大于 0")

        self._model = normalized_model
        self._max_tokens = max_tokens
        self._client = client or OpenAI(
            api_key=normalized_key,
            base_url=normalized_base_url,
            timeout=timeout_seconds,
            # 不在 SDK 层自动重试；调用失败由上层显式处理。
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return self._model

    @classmethod
    def from_settings(cls, settings: Settings) -> DeepSeekModelClient:
        """按应用配置构造客户端；缺少密钥时明确失败。"""

        if not settings.research_model_configured:
            raise ValueError(
                "未配置 DeepSeek API key；请设置 RC_RESEARCH_MODEL_API_KEY"
            )

        key = settings.research_model_api_key
        if key is None:  # 防御性检查，research_model_configured 已覆盖
            raise ValueError("未配置 DeepSeek API key")

        return cls(
            api_key=key.get_secret_value(),
            model=settings.research_model,
            base_url=settings.research_model_base_url,
            timeout_seconds=settings.research_model_timeout_seconds,
            max_tokens=settings.research_model_max_tokens,
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        """调用 DeepSeek Chat Completions，返回原始文本和最小元信息。"""

        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=request.temperature,
                max_tokens=self._max_tokens,
                stream=False,
            )
        except OpenAIError as exc:
            raise ModelCallError(
                f"DeepSeek API 调用失败：{type(exc).__name__}: {exc}"
            ) from exc

        choice = completion.choices[0] if completion.choices else None
        content = ""
        finish_reason: str | None = None
        if choice is not None:
            content = choice.message.content or ""
            finish_reason = choice.finish_reason

        usage: dict[str, int] = {}
        if completion.usage is not None:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value: Any = getattr(completion.usage, key, None)
                if isinstance(value, int):
                    usage[key] = value

        return ModelResponse(
            content=content,
            model=completion.model,
            finish_reason=finish_reason,
            usage=usage,
        )


__all__ = ["DeepSeekModelClient"]
