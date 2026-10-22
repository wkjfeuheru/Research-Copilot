"""模型客户端包。

定义最小结构化调用协议，并提供 DeepSeek OpenAI 兼容适配器。适配器把供应商错误
转换为 ``ModelCallError``；本模块不提供隐式 mock 或降级。
"""

from research_copilot.llm.client import (
    ModelCallError,
    ModelRequest,
    ModelResponse,
    StructuredModelClient,
)
from research_copilot.llm.deepseek import DeepSeekModelClient

__all__ = [
    "DeepSeekModelClient",
    "ModelCallError",
    "ModelRequest",
    "ModelResponse",
    "StructuredModelClient",
]
