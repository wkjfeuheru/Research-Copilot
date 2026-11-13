"""Two independent stores/workers; lease fencing and conservative recovery.

SQLite runs offline; PostgreSQL uses a new disposable database, never application
configuration. Extractors are blocking local fakes and make no provider calls.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import URL
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.experience import (
    ExtractionResult,
    RetrospectiveWorker,
    StaleClaim,
    enqueue_delivery,
    retrospective_jobs,
)
from research_copilot.runtime.retrospective_usage import record_attempt
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.runtime.urls import to_checkpoint_url, to_sqlalchemy_url
from tests.unit.test_experience import _delivered_state, _refs
from tests.unit.test_orchestration import context


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.db)])
async def lease_stores(request, tmp_path):
    admin = None
    name = "rc_lease_" + uuid4().hex
    if request.param == "postgres":
        base = os.environ.get("RC_TEST_DATABASE_URL")
        if not base:
            pytest.skip("Requires explicit isolated RC_TEST_DATABASE_URL")
        base = to_checkpoint_url(base)
        params = psycopg.conninfo.conninfo_to_dict(base)
        admin = await psycopg.AsyncConnection.connect(base, autocommit=True)
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        url = to_sqlalchemy_url(
            URL.create(
                "postgresql",
                username=params.get("user"),
                password=params.get("password"),
                host=params.get("host"),
                port=int(params.get("port", 5432)),
                database=name,
            ).render_as_string(hide_password=False)
        )
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'leases.db'}"
    engines = [create_async_engine(url), create_async_engine(url)]
    stores = [SqlSessionStore(engine) for engine in engines]
    try:
        await stores[0].setup()
        await stores[0].create(
            SessionRecord(
                "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
            ),
            TokenBudget(limit=100000, budget_id="budget-1").snapshot(),
        )
        plan, analysis, report = _refs()
        state = _delivered_state(report, analysis, plan)
        state.role_data["workflow_version"] = "products-v2"
        state.approved["report"] = report
        await stores[0].mutate_control(
            context(), lambda current: current.__dict__.update(state.__dict__)
        )
        async with engines[0].begin() as connection:
            await enqueue_delivery(
                connection,
                report_id="report-1",
                report_version="1",
                report_artifact_id=report.artifact_id,
                report_content_hash=report.content_hash,
                actor_id="actor-1",
            )
        yield stores
    finally:
        for engine in engines:
            await engine.dispose()
        if admin:
            assert name.startswith("rc_lease_") and name != params.get("dbname")
            try:
                await admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
            finally:
                await admin.close()


async def expire(store, claim):
    async with store.engine.begin() as connection:
        await connection.execute(
            retrospective_jobs.update()
            .where(retrospective_jobs.c.id == claim["id"])
            .values(locked_until=datetime(2000, 1, 1, tzinfo=UTC))
        )


async def owned_row(store):
    async with store.engine.connect() as connection:
        return (await connection.execute(select(retrospective_jobs))).mappings().one()


class BlockingExtractor:
    def __init__(self):
        self.entered = asyncio.Event()
        self.finish = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0

    async def extract(self, materials):
        self.calls += 1
        self.entered.set()
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ExtractionResult(
            drafts=(),
            usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        )


async def test_new_worker_start_preserves_live_claim(lease_stores):
    first, second = lease_stores
    claim = await first.experience.claim(lease_seconds=30)
    extractor = BlockingExtractor()
    worker = RetrospectiveWorker(second, extractor, poll_interval=0.05)
    try:
        await worker.start()
        await asyncio.sleep(0.15)
        job = await owned_row(first)
        assert job["claim_token"] == claim["claim_token"]
        assert job["attempts"] == 1 and extractor.calls == 0
    finally:
        await worker.stop()


async def test_heartbeat_keeps_long_extraction_exclusive(lease_stores):
    first, second = lease_stores
    extractor = BlockingExtractor()
    worker = RetrospectiveWorker(first, extractor, lease_seconds=1)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(extractor.entered.wait(), 5)
        before = await owned_row(first)
        await asyncio.sleep(1.4)
        assert await second.experience.claim() is None
        during = await owned_row(second)
        assert during["claim_token"] == before["claim_token"]
        assert during["locked_until"] > before["locked_until"]
        extractor.finish.set()
        assert await asyncio.wait_for(task, 5)
        job = (await second.experience.jobs("report-1"))[0]
        assert job["status"] == "completed" and job["attempts"] == 1
        assert job["usage"]["total_tokens"] == 12
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_expired_owner_cannot_publish_even_before_reclaim(lease_stores):
    first, _ = lease_stores
    claim = await first.experience.claim()
    await expire(first, claim)
    with pytest.raises(StaleClaim):
        await first.experience.complete(claim, [])
    with pytest.raises(StaleClaim):
        await record_attempt(first.engine, claim, status="started", usage={})
    assert (await first.experience.jobs("report-1"))[0]["status"] == "running"


async def test_reclaimed_unstarted_job_fences_old_owner(lease_stores):
    first, second = lease_stores
    claim = await first.experience.claim()
    await expire(first, claim)
    replacement = await second.experience.claim()
    assert replacement and replacement["claim_token"] != claim["claim_token"]
    await first.experience.release(claim)
    with pytest.raises(StaleClaim):
        await first.experience.complete(claim, [])
    with pytest.raises(StaleClaim):
        await first.experience.renew(claim)
    await second.experience.complete(replacement, [])
    job = (await first.experience.jobs("report-1"))[0]
    assert job["status"] == "completed" and job["attempts"] == 2


async def test_expired_unknown_request_requires_recovery_not_resend(lease_stores):
    first, second = lease_stores
    claim = await first.experience.claim()
    await record_attempt(first.engine, claim, status="started", usage={})
    await expire(first, claim)
    assert await second.experience.claim() is None
    job = (await second.experience.jobs("report-1"))[0]
    assert job["status"] == "failed" and "recovery_required" in job["last_error"]
    assert job["usage"]["unknown_attempts"] == 1 and job["attempts"] == 1


async def test_shutdown_unknown_attempt_is_not_automatically_retried(lease_stores):
    first, second = lease_stores
    extractor = BlockingExtractor()
    task = asyncio.create_task(RetrospectiveWorker(first, extractor).run_once())
    try:
        await asyncio.wait_for(extractor.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        job = (await second.experience.jobs("report-1"))[0]
        assert job["status"] == "failed" and "recovery_required" in job["last_error"]
        assert job["usage"]["attempts"][0]["status"] == "interrupted"
        assert await second.experience.claim() is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_lease_loss_cancels_local_work(lease_stores):
    first, second = lease_stores
    extractor = BlockingExtractor()
    task = asyncio.create_task(
        RetrospectiveWorker(first, extractor, lease_seconds=1).run_once()
    )
    try:
        await asyncio.wait_for(extractor.entered.wait(), 5)
        claim = (await second.experience.jobs("report-1"))[0]
        await expire(second, claim)
        await asyncio.wait_for(extractor.cancelled.wait(), 3)
        assert await asyncio.wait_for(task, 3)
        assert await second.experience.claim() is None
        job = (await second.experience.jobs("report-1"))[0]
        assert job["status"] == "failed" and job["usage"]["unknown_attempts"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_stores_claim_once(lease_stores):
    claims = await asyncio.gather(*(store.experience.claim() for store in lease_stores))
    assert sum(claim is not None for claim in claims) == 1


async def test_exhausted_unstarted_claim_is_not_retried(lease_stores):
    first, second = lease_stores
    claim = await first.experience.claim()
    async with first.engine.begin() as connection:
        await connection.execute(
            retrospective_jobs.update()
            .where(retrospective_jobs.c.id == claim["id"])
            .values(max_attempts=1)
        )
    await expire(first, claim)
    assert await second.experience.claim() is None
    job = (await first.experience.jobs("report-1"))[0]
    assert job["status"] == "failed" and job["attempts"] == 1


@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
async def test_failed_renewal_stops_extractor(lease_stores, monkeypatch, failure):
    first, _ = lease_stores
    extractor = BlockingExtractor()

    async def broken_renew(*args, **kwargs):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise ConnectionError("isolated renewal failure")

    monkeypatch.setattr(first.experience, "renew", broken_renew)
    task = asyncio.create_task(
        RetrospectiveWorker(first, extractor, lease_seconds=1).run_once()
    )
    try:
        await asyncio.wait_for(extractor.entered.wait(), 5)
        with pytest.raises((TimeoutError, ConnectionError)):
            await asyncio.wait_for(task, 3)
        assert extractor.cancelled.is_set()
        job = (await first.experience.jobs("report-1"))[0]
        assert job["status"] == "failed" and job["usage"]["unknown_attempts"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_two_process_workers_keep_live_owner_and_recover_killed_unknown(
    lease_stores, tmp_path
):
    first, _ = lease_stores
    if first.engine.dialect.name != "postgresql":
        pytest.skip("OS-process acceptance uses PostgreSQL")
    children = []
    logs = []
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("RC_")
    }
    env["RC_TEST_DATABASE_URL"] = first.engine.url.render_as_string(hide_password=False)

    async def wait_file(child, path):
        async with asyncio.timeout(10):
            while not path.exists():
                assert child.poll() is None, (
                    "Worker exited; inspect preserved child log"
                )
                await asyncio.sleep(0.05)

    def launch(name):
        directory = tmp_path / name
        directory.mkdir()
        log = (directory / "child.log").open("wb")
        logs.append(log)
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.fakes.retrospective_process", str(directory)],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        children.append(child)
        return child, directory

    try:
        owner, owner_root = launch("owner")
        await wait_file(owner, owner_root / "entered")
        before = await owned_row(first)
        follower, follower_root = launch("follower")
        await wait_file(follower, follower_root / "ready")
        await asyncio.sleep(2.4)
        during = await owned_row(first)
        assert owner.poll() is None and follower.poll() is None
        assert (
            during["claim_token"] == before["claim_token"] and during["attempts"] == 1
        )
        assert during["locked_until"] > before["locked_until"]
        assert not (follower_root / "entered").exists()
        owner.kill()
        owner.wait(timeout=5)
        async with asyncio.timeout(8):
            while True:
                job = (await first.experience.jobs("report-1"))[0]
                if job["status"] == "failed":
                    break
                assert follower.poll() is None
                await asyncio.sleep(0.05)
        assert "recovery_required" in job["last_error"]
        assert job["attempts"] == 1 and job["usage"]["unknown_attempts"] == 1
        assert not (follower_root / "entered").exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        for log in logs:
            log.close()


async def test_postgres_lease_uses_database_clock(lease_stores, monkeypatch):
    first, second = lease_stores
    if first.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL clock contract")
    import research_copilot.runtime.experience as module

    claim = await first.experience.claim(lease_seconds=30)
    monkeypatch.setattr(
        module, "_utc_now", lambda: datetime.now(UTC) + timedelta(days=30)
    )
    assert await second.experience.claim() is None
    await first.experience.renew(claim, lease_seconds=30)
    job = (await first.experience.jobs("report-1"))[0]
    assert datetime.fromisoformat(job["locked_until"]) < datetime.now(UTC) + timedelta(
        minutes=1
    )
    # A skewed enqueueing process must not postpone new work for a month either.
    async with first.engine.begin() as connection:
        await enqueue_delivery(
            connection,
            report_id="report-1",
            report_version="2",
            report_artifact_id="report-2",
            report_content_hash="d" * 64,
            actor_id="actor-1",
        )
    newly_queued = await second.experience.claim()
    assert newly_queued and newly_queued["report_version"] == "2"
