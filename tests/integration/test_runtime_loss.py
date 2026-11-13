"""Connected host loses its guard: stop workers and API admission, retain audit."""

import asyncio

import httpx
import psycopg
import pytest

from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.urls import to_checkpoint_url
from research_copilot.runtime.runtime_ownership import list_owners, recover_owner
from research_copilot.runtime.experience import RetrospectiveWorker
from research_copilot.web.app import WorkbenchBackend, create_app
from tests.integration import test_retrospective_leases as fixtures
from tests.integration.test_run_worker import BlockingSession, command

lease_stores = fixtures.lease_stores
pytestmark = pytest.mark.db


async def test_lost_guard_stops_research_retrospective_and_web_admission(
    lease_stores, tmp_path
):
    first = lease_stores[0]
    if first.engine.dialect.name != "postgresql":
        pytest.skip("Actual owning connection loss requires PostgreSQL")
    url = first.engine.url.render_as_string(hide_password=False)
    async with postgres_runtime(url, initialize=True):
        pass
    with pytest.raises(RuntimeError, match="ownership"):
        async with postgres_runtime(url) as (store, _):
            session = BlockingSession()
            session.store = store
            extractor = fixtures.BlockingExtractor()
            retrospective = RetrospectiveWorker(store, extractor, poll_interval=0.05)
            backend = WorkbenchBackend(
                session=session,
                store=store,
                actor_id="actor-1",
                workspace_root=tmp_path,
            )
            app = create_app(backend=backend)
            job = await command(backend.runs.queue)
            try:
                await backend.runs.start()
                await retrospective.start()
                await asyncio.wait_for(session.entered.wait(), 5)
                await asyncio.wait_for(extractor.entered.wait(), 5)
                owner = (await list_owners(url))[0]
                async with await psycopg.AsyncConnection.connect(
                    to_checkpoint_url(url), autocommit=True
                ) as admin:
                    await admin.execute(
                        "SELECT pg_terminate_backend(%s,5000)", (owner["backend_pid"],)
                    )
                await asyncio.wait_for(session.cancelled.wait(), 8)
                await asyncio.wait_for(extractor.cancelled.wait(), 8)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost"
                ) as client:
                    assert (await client.get("/api/ready")).status_code == 503
                    assert (await client.get("/api/live")).status_code == 200
                    assert (await client.get("/api/reports")).status_code == 503
                    assert (
                        await client.post(
                            "/api/reports", json={"user_request": "Must not enter"}
                        )
                    ).status_code == 503
                with pytest.raises(Exception, match="关闭|ownership"):
                    await backend.runs.submit("report-1", "resume", {"decision": None})
                assert session.calls == 1 and extractor.calls == 1
                await backend.runs.worker.drain()
                assert (await backend.runs.queue.get(job["id"], "actor-1"))[
                    "status"
                ] == "failed"
            finally:
                session.release.set()
                extractor.finish.set()
                await backend.runs.aclose()
                await retrospective.stop()
    unknown = (await list_owners(url))[0]
    assert unknown["id"] == owner["id"] and not unknown["connected"]
    await recover_owner(
        url,
        owner["id"],
        actor_id="test-operator",
        note="Both fake executions drained and owning context exited",
        owner_stopped=True,
    )


async def test_cli_wait_exits_after_guard_loss_and_drains_execution(lease_stores):
    from research_copilot.runtime.local_runner import run_command
    from research_copilot.runtime.run_queue import RunQueue

    first = lease_stores[0]
    if first.engine.dialect.name != "postgresql":
        pytest.skip("Actual owning connection loss requires PostgreSQL")
    url = first.engine.url.render_as_string(hide_password=False)
    async with postgres_runtime(url, initialize=True):
        pass
    with pytest.raises(RuntimeError, match="ownership"):
        async with postgres_runtime(url) as (store, _):
            session = BlockingSession()
            session.store = store
            queue = RunQueue(store.engine)
            job = await command(queue)
            task = asyncio.create_task(run_command(queue, session, job))
            try:
                await asyncio.wait_for(session.entered.wait(), 5)
                owner = (await list_owners(url))[0]
                async with await psycopg.AsyncConnection.connect(
                    to_checkpoint_url(url), autocommit=True
                ) as admin:
                    await admin.execute(
                        "SELECT pg_terminate_backend(%s,5000)", (owner["backend_pid"],)
                    )
                with pytest.raises(RuntimeError, match="ownership"):
                    await asyncio.wait_for(task, 15)
                assert session.cancelled.is_set() and session.calls == 1
                assert (await queue.get(job["id"], "actor-1"))["status"] == "failed"
            finally:
                session.release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert (await list_owners(url))[0]["state"] == "open"


async def test_probe_timeout_remains_lost_after_database_query_recovers(lease_stores):
    first = lease_stores[0]
    if first.engine.dialect.name != "postgresql":
        pytest.skip("Actual owning connection loss requires PostgreSQL")
    url = first.engine.url.render_as_string(hide_password=False)
    async with postgres_runtime(url, initialize=True):
        pass
    with pytest.raises(RuntimeError, match="ownership"):
        async with postgres_runtime(url) as (store, _):
            async with await psycopg.AsyncConnection.connect(
                to_checkpoint_url(url)
            ) as blocker:
                await blocker.execute(
                    "LOCK TABLE runtime_owners IN ACCESS EXCLUSIVE MODE"
                )
                try:
                    await asyncio.wait_for(store.runtime_watch.lost.wait(), 6)
                finally:
                    await blocker.rollback()
            # The same owning connection is still connected after query cancellation,
            # but a successful later query must never silently reopen admission.
            owner = (await list_owners(url))[0]
            assert owner["connected"] is True
            with pytest.raises(RuntimeError, match="ownership"):
                store.runtime_watch.check()
    owner = (await list_owners(url))[0]
    assert owner["state"] == "open" and owner["connected"] is False
