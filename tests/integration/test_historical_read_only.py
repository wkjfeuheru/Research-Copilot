"""Legacy session reads never initialize models; every HTTP mutation is denied."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord, SessionService
from research_copilot.web.app import WorkbenchBackend, create_app


@pytest.mark.parametrize(
    "version,history_only",
    [
        (None, False),
        ("products-v1", False),
        ("supervisor-v1", False),
        ("products-v2", True),
    ],
)
async def test_history_is_readable_and_all_write_routes_are_closed(
    tmp_path, version, history_only
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord("history", "local", "v2", "policy", "history", run_id="old"),
        TokenBudget(limit=1000, budget_id="history").snapshot(),
        workflow_version=version,
    )

    def unavailable(*args):
        raise AssertionError("History must not initialize a model graph")

    session = SessionService(
        store=store, agent_factory=unavailable, policy_version="policy"
    )
    backend = WorkbenchBackend(
        session=session,
        store=store,
        actor_id="local",
        workspace_root=tmp_path,
        policy_version="policy",
        history_only=history_only,
    )
    before = await store.read_snapshot("history", "local")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(backend=backend)),
            base_url="http://localhost",
        ) as client:
            page = (await client.get("/api/reports/history")).json()
            assert (
                page["read_only"] and page["actions"] == [] and page["interrupts"] == []
            )
            for method, path in [
                ("POST", "resume"),
                ("POST", "messages"),
                ("POST", "decisions"),
                ("POST", "cancel"),
                ("POST", "correct"),
                ("POST", "inputs"),
                ("POST", "runs/job/release"),
                ("DELETE", ""),
            ]:
                response = await client.request(
                    method,
                    "/api/reports/history" + ("/" + path if path else ""),
                    json={},
                )
                assert response.status_code == 409, (method, path, response.text)
            assert (await store.read_snapshot("history", "local")).state == before.state
    finally:
        await backend.runs.aclose()
        await engine.dispose()
