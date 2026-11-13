"""Durable admission contract, with independent SQLite/PostgreSQL connections.

No graph, model, source or business database is involved in this storage layer.
"""

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.run_queue import (
    QueueConflict,
    QueueFull,
    RunQueue,
    StaleRunClaim,
    run_jobs,
    run_queue_settings,
)
from research_copilot.runtime.session import SessionRecord
from tests.integration import test_retrospective_leases as lease_fixtures

lease_stores = lease_fixtures.lease_stores


async def add_report(store, report_id):
    await store.create(
        SessionRecord(report_id, "actor-1", "v2", "policy-1", report_id),
        TokenBudget(limit=1000, budget_id=report_id).snapshot(),
    )


async def enqueue(queue, report="report-1", **kwargs):
    return await queue.enqueue(
        report_id=report,
        actor_id="actor-1",
        operation_id=kwargs.pop("operation_id", uuid4().hex),
        kind="resume",
        payload=kwargs.pop("payload", {"decision": None}),
        **kwargs,
    )


async def expire(store, job):
    async with store.engine.begin() as conn:
        await conn.execute(
            update(run_jobs)
            .where(run_jobs.c.id == job["id"])
            .values(locked_until=datetime(2000, 1, 1, tzinfo=UTC))
        )


async def test_enqueue_survives_reopen_and_is_idempotent(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    key = uuid4().hex
    job = await enqueue(first, operation_id=key)
    same = await enqueue(second, operation_id=key)
    assert same["id"] == job["id"]
    assert same["payload"] == {"decision": None}
    assert same["run_id"] == "run-1" and same["budget_id"] == "budget-1"
    with pytest.raises(QueueConflict):
        await enqueue(second, operation_id=key, payload={"decision": {"x": True}})
    with pytest.raises(QueueConflict):
        await enqueue(second)
    with pytest.raises(LookupError):
        await second.get(job["id"], "another-actor")
    with pytest.raises(LookupError):
        await second.enqueue(
            report_id="report-1",
            actor_id="another-actor",
            operation_id=key,
            kind="resume",
            payload={},
        )


async def test_global_admission_and_completion_release(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    for i in range(5):
        await add_report(lease_stores[0], f"queue-{i}")
        await enqueue(first, f"queue-{i}")
    results = await asyncio.gather(
        *(queue.claim(f"worker-{i}") for i, queue in enumerate([first, second] * 3))
    )
    claims = [job for job in results if job]
    assert len(claims) == 3
    assert len({job["id"] for job in claims}) == 3
    assert await second.claim("full-worker") is None
    with pytest.raises(StaleRunClaim):
        await first.finish(claims[0], succeeded=True)
    running = await first.start(claims[0])
    assert running["status"] == "running"
    await first.finish(running, succeeded=True)
    assert await second.claim("replacement") is not None


async def test_only_unstarted_expired_claim_is_reissued(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    await enqueue(first)
    old = await first.claim("dead-worker")
    await expire(lease_stores[0], old)
    new = await second.claim("new-worker")
    assert new["id"] == old["id"] and new["claim_token"] != old["claim_token"]
    for method in (first.start, first.renew):
        with pytest.raises(StaleRunClaim):
            await method(old)
    await second.start(new)
    await expire(lease_stores[0], new)
    with pytest.raises(StaleRunClaim):
        await second.finish(new, succeeded=True)
    assert await first.claim("third-worker") is None
    result = await first.get(new["id"], "actor-1")
    assert result["status"] == "recovery_required"
    assert result["claim_token"] == new["claim_token"]
    with pytest.raises(QueueConflict):
        await enqueue(first)


async def test_expired_running_keeps_global_slot(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    async with lease_stores[0].engine.begin() as conn:
        await conn.execute(update(run_queue_settings).values(max_active=1))
    await enqueue(first)
    old = await first.claim("uncertain-owner")
    await first.start(old)
    await expire(lease_stores[0], old)
    await add_report(lease_stores[0], "another-report")
    await enqueue(second, "another-report")
    assert await second.claim("new-worker") is None
    assert (await first.get(old["id"], "actor-1"))["status"] == "recovery_required"


async def test_defer_only_releases_unstarted_current_owner(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    await enqueue(first)
    old = await first.claim("busy-worker")
    pending = await first.defer(old)
    assert pending["status"] == "pending" and pending["claim_token"] is None
    current = await second.claim("new-worker")
    assert current["id"] == old["id"]
    with pytest.raises(StaleRunClaim):
        await first.defer(old)
    await second.start(current)
    with pytest.raises(StaleRunClaim):
        await second.defer(current)


async def test_pending_backpressure_and_cancel_are_shared(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    async with lease_stores[0].engine.begin() as conn:
        await conn.execute(update(run_queue_settings).values(max_pending=1))
    job = await enqueue(first)
    await add_report(lease_stores[0], "another-report")
    with pytest.raises(QueueFull):
        await enqueue(second, "another-report")
    assert await second.cancel_waiting("report-1", "another-actor") == 0
    assert await second.cancel_waiting("report-1", "actor-1") == 1
    assert (await first.get(job["id"], "actor-1"))["status"] == "cancelled"
    await enqueue(second, "another-report")
    claim = await first.claim("worker")
    assert await second.cancel_waiting("another-report", "actor-1") == 1
    with pytest.raises(StaleRunClaim):
        await first.start(claim)


async def test_renewal_and_running_cancel_do_not_release_ownership(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    await enqueue(first)
    claim = await first.claim("worker", lease_seconds=5)
    renewed = await second.renew(claim, lease_seconds=30)
    assert renewed["locked_until"] > claim["locked_until"]
    await first.start(claim)
    assert await second.cancel_waiting("report-1", "actor-1") == 0
    done = await first.finish(claim, succeeded=False, error="RunCancelled")
    assert done["status"] == "failed" and done["error"] == "RunCancelled"
    with pytest.raises(StaleRunClaim):
        await second.renew(claim)
    assert await second.claim("worker") is None


async def test_enqueue_participates_in_callers_transaction(lease_stores):
    queue = RunQueue(lease_stores[0].engine)
    with pytest.raises(RuntimeError, match="rollback"):
        async with queue.transaction() as conn:
            await queue.enqueue_in(
                conn,
                report_id="report-1",
                actor_id="actor-1",
                operation_id=uuid4().hex,
                kind="resume",
                payload={"decision": None},
            )
            raise RuntimeError("rollback")
    async with lease_stores[1].engine.connect() as conn:
        assert (await conn.execute(select(run_jobs))).first() is None
    assert await RunQueue(lease_stores[1].engine).claim("worker") is None


async def test_concurrent_submissions_are_idempotent_and_bounded(lease_stores):
    first, second = (RunQueue(store.engine) for store in lease_stores)
    key = uuid4().hex
    same = await asyncio.gather(
        *(enqueue(queue, operation_id=key) for queue in [first, second] * 3)
    )
    assert len({job["id"] for job in same}) == 1
    await first.cancel_waiting("report-1", "actor-1")
    await add_report(lease_stores[0], "another-report")
    async with lease_stores[0].engine.begin() as conn:
        await conn.execute(update(run_queue_settings).values(max_pending=1))
    results = await asyncio.gather(
        enqueue(first), enqueue(second, "another-report"), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, QueueFull) for result in results) == 1


async def test_cancelled_report_cannot_be_dispatched(lease_stores):
    from research_copilot.runtime.storage import sessions

    queue = RunQueue(lease_stores[0].engine)
    job = await enqueue(queue)
    async with lease_stores[0].engine.begin() as conn:
        await conn.execute(update(sessions).values(cancel_requested=True))
    assert await queue.claim("worker") is None
    assert (await queue.get(job["id"], "actor-1"))["status"] == "cancelled"
    with pytest.raises(QueueConflict, match="Cancelled"):
        await enqueue(queue)


@pytest.mark.parametrize("started", [False, True])
async def test_two_os_processes_and_owner_death(lease_stores, tmp_path, started):
    store = lease_stores[0]
    if store.engine.dialect.name != "postgresql":
        pytest.skip("OS process acceptance requires isolated PostgreSQL")
    queue = RunQueue(store.engine)
    job = await enqueue(queue)
    async with store.engine.begin() as conn:
        await conn.execute(update(run_queue_settings).values(max_active=1))
    env = {
        **os.environ,
        "RC_TEST_DATABASE_URL": store.engine.url.render_as_string(hide_password=False),
    }
    children, logs = [], []
    roots = [tmp_path / "owner", tmp_path / "follower"]

    async def spawn(root):
        root.mkdir()
        log = (root / "worker.log").open("w", encoding="utf-8")
        logs.append(log)
        args = [sys.executable, "-m", "tests.fakes.run_queue_process", str(root)]
        if started:
            args.append("--start")
        child = subprocess.Popen(
            args,
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        children.append(child)
        async with asyncio.timeout(15):
            while not (root / "ready").exists():
                assert child.poll() is None
                await asyncio.sleep(0.05)
        return child

    try:
        owner = await spawn(roots[0])
        async with asyncio.timeout(5):
            while not (roots[0] / "claim.json").exists():
                assert owner.poll() is None
                await asyncio.sleep(0.05)
        follower = await spawn(roots[1])
        await asyncio.sleep(1.8)
        assert owner.poll() is None and follower.poll() is None
        assert not (roots[1] / "claim.json").exists()
        owner.kill()
        owner.wait(timeout=5)
        async with asyncio.timeout(8):
            while True:
                row = await queue.get(job["id"], "actor-1")
                if started and row["status"] == "recovery_required":
                    assert not (roots[1] / "claim.json").exists()
                    break
                if not started and (roots[1] / "claim.json").exists():
                    recovered = json.loads(
                        (roots[1] / "claim.json").read_text(encoding="utf-8")
                    )
                    previous = json.loads(
                        (roots[0] / "claim.json").read_text(encoding="utf-8")
                    )
                    assert recovered["id"] == previous["id"] == job["id"]
                    assert recovered["claim_token"] != previous["claim_token"]
                    break
                assert follower.poll() is None
                await asyncio.sleep(0.05)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        for log in logs:
            log.close()
