"""最小结构化模型调用边界。

Planner 只依赖本模块的协议，不依赖任何具体模型供应商。客户端返回原始文本；
JSON 解析、Schema 校验和业务校验由 Agent 负责，避免把"结构化失败"隐藏在客户端里。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """一次结构化生成请求。

    ``response_schema`` 是提供给模型或适配器的 JSON Schema；客户端仍应返回
    原始文本，以便调用方执行自己的严格校验。
    """

    system_prompt: str
    user_prompt: str
    response_schema: dict[str, Any]
    prompt_version: str
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """模型返回的原始内容和最小元信息。"""

    content: str
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class ModelCallError(RuntimeError):
    """模型适配器在调用失败时应抛出的可识别异常。"""


class StructuredModelClient(Protocol):
    """Planner 依赖的结构化模型客户端协议。"""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """执行一次调用，返回原始文本；失败时抛出 ``ModelCallError``。"""


__all__ = [
    "ModelCallError",
    "ModelRequest",
    "ModelResponse",
    "StructuredModelClient",
]
