"""健康检查响应契约。

健康响应只描述 API 进程及当前明确未接入的能力。数据库、模型和研究链路的
状态不能由本接口推断为可用。
"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CheckStatus(str, Enum):
    """单项检查状态。"""

    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    NOT_CHECKED = "not_checked"


class HealthCheck(BaseModel):
    """单项能力状态及说明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CheckStatus
    detail: str = Field(min_length=1)


class HealthChecks(BaseModel):
    """阶段一固定的检查项集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api: HealthCheck
    database: HealthCheck
    model: HealthCheck
    workflow: HealthCheck


class HealthResponse(BaseModel):
    """``GET /api/health`` 的响应。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    checked_at: datetime
    message: str
    checks: HealthChecks


__all__ = ["CheckStatus", "HealthCheck", "HealthChecks", "HealthResponse"]
