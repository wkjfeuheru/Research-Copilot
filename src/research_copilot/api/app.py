"""FastAPI 应用工厂。"""

from fastapi import FastAPI

from research_copilot import __version__
from research_copilot.api.router import api_router
from research_copilot.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建应用；测试可注入独立配置。"""

    resolved = settings or get_settings()
    expose_docs = resolved.environment != "production"
    application = FastAPI(
        title=resolved.app_name,
        version=__version__,
        description=(
            "Research Copilot 阶段二工程。当前仅提供健康检查；"
            "研究主链路实现了 Planner 的单节点图，并提供 DeepSeek 模型适配器；"
            "研究 HTTP 链路、数据工具和持久化尚未接入。"
        ),
        docs_url=f"{resolved.api_prefix}/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url=f"{resolved.api_prefix}/openapi.json" if expose_docs else None,
    )
    application.state.settings = resolved
    application.include_router(api_router, prefix=resolved.api_prefix)
    return application


app = create_app()

__all__ = ["create_app", "app"]
