from types import SimpleNamespace

import httpx

from research_copilot.runtime.runtime_watch import RuntimeWatch
from research_copilot.web.app import create_app


async def test_normal_runtime_close_refuses_api_before_accessing_dependencies():
    calls = []

    async def probe():
        return

    async def rows(*args):
        calls.append("database")
        return []

    watch = RuntimeWatch(probe)
    await watch.close()
    assert not watch.lost.is_set()
    backend = SimpleNamespace(
        actor_id="local",
        store=SimpleNamespace(runtime_watch=watch, list_for_actor=rows),
        runs=SimpleNamespace(queue=SimpleNamespace(active_reports=rows)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend=backend)),
        base_url="http://localhost",
    ) as client:
        response = await client.get("/api/reports")
    assert response.status_code == 503
    assert calls == []
