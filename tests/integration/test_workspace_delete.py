import httpx
import asyncio
import threading
import pytest

from research_copilot.web.app import WorkbenchBackend, create_app
from research_copilot.runtime.storage import SessionBusy
from tests.integration import test_retrospective_leases as fixtures
from tests.integration.test_web_app import FakeSession, POLICY

lease_stores = fixtures.lease_stores


async def test_delete_refuses_while_report_file_operation_holds_its_lock(
    lease_stores, tmp_path
):
    store = lease_stores[0]
    current = WorkbenchBackend(
        session=FakeSession(store),
        store=store,
        actor_id="actor-1",
        workspace_root=tmp_path,
        execution_policy=POLICY,
    )
    root = tmp_path / "report-1"
    root.mkdir()
    (root / "input.txt").write_text("Retain active input", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend=current)),
        base_url="http://localhost",
    ) as client:
        async with store.lock("report-1"):
            response = await client.delete("/api/reports/report-1")
            assert response.status_code == 409
        assert (root / "input.txt").read_text("utf-8") == "Retain active input"
        assert (await store.load("report-1", "actor-1")).report_id == "report-1"


async def test_cancelled_delete_keeps_lock_until_cleanup_thread_returns(
    lease_stores, tmp_path, monkeypatch
):
    from research_copilot.web import app as module

    store = lease_stores[0]
    current = WorkbenchBackend(
        session=FakeSession(store),
        store=store,
        actor_id="actor-1",
        workspace_root=tmp_path,
        execution_policy=POLICY,
    )
    (tmp_path / "report-1").mkdir()
    entered, finish = threading.Event(), threading.Event()
    real_cleanup = module._remove_workspace

    def cleanup(*args):
        entered.set()
        assert finish.wait(8)
        return real_cleanup(*args)

    monkeypatch.setattr(module, "_remove_workspace", cleanup)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend=current)),
        base_url="http://localhost",
    ) as client:
        task = asyncio.create_task(client.delete("/api/reports/report-1"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0.02)
            with pytest.raises(SessionBusy):
                async with store.lock("report-1"):
                    pytest.fail(
                        "Cancelled deletion released its lock before file cleanup"
                    )
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert not (tmp_path / "report-1").exists()
