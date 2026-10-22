"""聚合 API 路由。"""

from fastapi import APIRouter

from research_copilot.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)

__all__ = ["api_router"]
