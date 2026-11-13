import httpx
import pytest
from fastapi import FastAPI
from tests.unit.test_orchestration import context


@pytest.mark.asyncio
async def test_status_extension_reads_budget_without_graph_execution_and_hides_other_actor(
    role_env,
):
    from research_copilot.web.charts import ChartService, create_chart_router

    async def actor():
        return "actor-1"

    app = FastAPI()
    app.include_router(create_chart_router(ChartService(role_env[0]), actor))
    budget = await role_env[2].load_budget(context())
    key = await budget.reserve(100, stage="analysist")
    await budget.settle(key, None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/reports/report-1/observability")
        assert response.status_code == 200
        body = response.json()
        assert body["runtime_version"] == "v2"
        assert body["budget"]["known_actual_tokens"] == 0
        assert body["budget"]["unknown_reserved_tokens"] == 100
        assert body["signed"] is False
        app.dependency_overrides[actor] = lambda: "other"
        assert (
            await client.get("/api/reports/report-1/observability")
        ).status_code == 404
