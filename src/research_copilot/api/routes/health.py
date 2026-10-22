"""健康检查路由。"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from research_copilot import __version__
from research_copilot.api.schemas.health import (
    CheckStatus,
    HealthCheck,
    HealthChecks,
    HealthResponse,
)
from research_copilot.config import Settings

router = APIRouter(tags=["system"])

_PROCESS_ONLY_MESSAGE = (
    "API 进程可用；数据库、模型与研究链路尚未接入，"
    "本响应不表示研究能力就绪。"
)


def _database_check(settings: Settings) -> HealthCheck:
    if settings.database_configured:
        return HealthCheck(
            status=CheckStatus.NOT_CHECKED,
            detail="已提供数据库配置；本阶段不建立连接、不验证可用性。",
        )
    return HealthCheck(
        status=CheckStatus.NOT_CONFIGURED,
        detail="未配置数据库；本阶段不访问 PostgreSQL 或 pgvector。",
    )


def _model_check(settings: Settings) -> HealthCheck:
    if settings.research_model_configured:
        return HealthCheck(
            status=CheckStatus.NOT_CHECKED,
            detail="已提供研究模型配置；本阶段不发起任何模型调用。",
        )
    return HealthCheck(
        status=CheckStatus.NOT_CONFIGURED,
        detail="未完整配置研究模型；本阶段不发起任何模型调用。",
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="骨架服务健康检查",
    description=(
        "仅确认 HTTP 进程可响应。数据库、模型和研究链路在本阶段均未接入，"
        "响应中的状态不会被报告为可用。"
    ),
)
def read_health(request: Request) -> HealthResponse:
    """返回进程状态和明确未接入的依赖状态。"""

    settings: Settings = request.app.state.settings
    return HealthResponse(
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        checked_at=datetime.now(UTC),
        message=_PROCESS_ONLY_MESSAGE,
        checks=HealthChecks(
            api=HealthCheck(
                status=CheckStatus.OK,
                detail="HTTP 应用已启动，可响应健康检查。",
            ),
            database=_database_check(settings),
            model=_model_check(settings),
            workflow=HealthCheck(
                status=CheckStatus.NOT_CONFIGURED,
                detail="LangGraph 研究链路尚未实现；本接口不触发任何编排。",
            ),
        ),
    )


__all__ = ["router"]
