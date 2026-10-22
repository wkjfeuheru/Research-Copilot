"""模型客户端包。

阶段二只定义最小结构化调用协议，不绑定模型供应商。后续适配器在此实现，
并负责把供应商错误转换为 ``ModelCallError``；本模块不提供隐式 mock 或降级。
"""

from research_copilot.llm.client import (
    ModelCallError,
    ModelRequest,
    ModelResponse,
    StructuredModelClient,
)

__all__ = [
    "ModelCallError",
    "ModelRequest",
    "ModelResponse",
    "StructuredModelClient",
]
