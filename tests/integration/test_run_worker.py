"""Queue execution through SessionService; deterministic graph and model only."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
import pytest
from sqlalchemy import update

from research_copilot.runtime.run_queue import RunQueue, run_jobs
from research_copilot.runtime.run_worker import RunWorker
from research_copilot.runtime.session import SessionService
from tests.integration import test_retrospective_leases as lease_fixtures
from tests.integration.test_run_queue import add_report

lease_stores = lease_fixtures.lease_stores


async def command(queue, *, report="report-1", kind="resume", payload=None):
    return await queue.enqueue(
        report_id=report,
        actor_id="actor-1",
        operation_id=uuid4().hex,
        kind=kind,
        payload=payload if payload is not None else {"decision": None},
    )


async def test_durable_start_and_resume_use_real_session_and_interrupt_validator(
    lease_stores,
):
    store = lease_stores[0]
    await add_report(store, "fresh")
    record = await store.load("fresh", "actor-1")
    # create() captures the submitted request; no role outputs or grants are seeded.
    from research_copilot.runtime.storage import sessions

    async with store.engine.begin() as conn:
        await conn.execute(
            update(sessions)
            .where(sessions.c.report_id == "fresh")
            .values(user_request="research")
        )
    approvals, graph_results = [], []

    async def validate(context, pending, decisions):
        approvals.append((context.run_id, pending, decisions))

    store.resume_validator = validate

    def gate(state):
        decision = interrupt(
            {"action_requests": [{"name": "confirm_plan", "args": {}}]}
        )
        graph_results.append(decision)
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("gate", gate)
    graph.add_edge(START, "gate")
    graph.add_edge("gate", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    session = SessionService(
        store=store, agent_factory=lambda *_: compiled, policy_version="policy-1"
    )
    first = RunQueue(store.engine)
    start = await command(
        first, report="fresh", kind="start", payload={"message": "research"}
    )
    # A new queue/worker object consumes the persisted command without re-submission.
    worker = RunWorker(RunQueue(lease_stores[1].engine), session)
    assert await worker.run_once()
    assert (await first.get(start["id"], "actor-1"))["status"] == "succeeded"
    status = await session.get_status("fresh", "actor-1")
    assert status.status == "paused" and len(status.interrupts) == 1
    started = await store.load("fresh", "actor-1")
    assert started.run_id and not record.run_id
    assert started.budget_id == record.budget_id
    assert approvals == [] and graph_results == []
    bad = await command(
        first,
        report="fresh",
        payload={"decision": {"old-interrupt": {"decisions": [{"type": "approve"}]}}},
    )
    assert await worker.run_once()
    assert (await first.get(bad["id"], "actor-1"))["status"] == "failed"
    assert approvals == [] and graph_results == []
    decisions = {status.interrupts[0]["id"]: {"decisions": [{"type": "approve"}]}}
    good = await command(first, report="fresh", payload={"decision": decisions})
    assert await worker.run_once()
    assert (await first.get(good["id"], "actor-1"))["status"] == "succeeded"
    assert len(approvals) == len(graph_results) == 1
    assert approvals[0][0] == started.run_id
    assert (await store.load("fresh", "actor-1")).run_id == started.run_id
    assert not await worker.run_once()
    message = await command(
        first,
        report="fresh",
        kind="message",
        payload={"message": "follow up", "message_id": "message-1"},
    )
    assert await worker.run_once()
    assert (await first.get(message["id"], "actor-1"))["status"] == "succeeded"
    assert (await session.get_status("fresh", "actor-1")).interrupts
    assert len(approvals) == len(graph_results) == 1


async def test_identity_drift_and_malformed_command_never_invoke_graph(lease_stores):
    store = lease_stores[0]
    queue = RunQueue(store.engine)

    def forbidden(*_):
        pytest.fail("Invalid queued command reached graph construction")

    session = SessionService(
        store=store, agent_factory=forbidden, policy_version="policy-1"
    )
    worker = RunWorker(queue, session)
    job = await command(queue)
    record = await store.load("report-1", "actor-1")
    await store.save(replace(record, run_id="different-run"))
    assert await worker.run_once()
    rejected = await queue.get(job["id"], "actor-1")
    assert rejected["status"] == "failed" and rejected["started_at"] is None
    malformed = await command(
        queue, payload={"decision": None, "unapproved_input": "inject"}
    )
    assert await worker.run_once()
    assert (await queue.get(malformed["id"], "actor-1"))["status"] == "failed"


async def test_busy_report_lock_defers_unstarted_command_without_losing_it(
    lease_stores,
):
    from unittest.mock import AsyncMock

    store = lease_stores[0]
    session = SessionService(
        store=store, agent_factory=lambda *_: None, policy_version="policy-1"
    )
    session._resume = AsyncMock()
    queue = RunQueue(store.engine)
    job = await command(queue)
    worker = RunWorker(queue, session, poll_interval=0.01)
    async with store.lock("report-1"):
        assert await worker.run_once()
        pending = await queue.get(job["id"], "actor-1")
        assert pending["status"] == "pending" and pending["started_at"] is None
        session._resume.assert_not_awaited()
    assert await worker.run_once()
    session._resume.assert_awaited_once()
    assert (await queue.get(job["id"], "actor-1"))["status"] == "succeeded"


async def test_busy_after_execution_barrier_is_never_requeued(lease_stores):
    from unittest.mock import AsyncMock

    from research_copilot.runtime.storage import SessionBusy

    store = lease_stores[0]
    session = SessionService(
        store=store, agent_factory=lambda *_: None, policy_version="policy-1"
    )
    session._resume = AsyncMock(side_effect=SessionBusy("already started"))
    queue = RunQueue(store.engine)
    job = await command(queue)
    worker = RunWorker(queue, session, poll_interval=0.01)
    assert await worker.run_once()
    result = await queue.get(job["id"], "actor-1")
    assert result["status"] == "failed" and result["started_at"] is not None
    assert not await worker.run_once()
    session._resume.assert_awaited_once()


class BlockingSession:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0
        self.stopping = False

    def stop_runs(self):
        self.stopping = True

    async def execute_command(self, job, *, before_execute):
        await before_execute()
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.cancelled.set()


async def test_worker_renews_long_command_and_second_worker_does_not_execute(
    lease_stores,
):
    queue = RunQueue(lease_stores[0].engine)
    job = await command(queue)
    session = BlockingSession()
    worker = RunWorker(queue, session, lease_seconds=1.2)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(session.entered.wait(), 3)
        await asyncio.sleep(1.6)
        follower_session = BlockingSession()
        follower = RunWorker(RunQueue(lease_stores[1].engine), follower_session)
        assert not await follower.run_once()
        assert follower_session.calls == 0 and session.calls == 1
    finally:
        session.release.set()
        await task
    assert (await queue.get(job["id"], "actor-1"))["status"] == "succeeded"


async def test_lease_loss_cancels_execution_and_does_not_publish_success(lease_stores):
    queue = RunQueue(lease_stores[0].engine)
    job = await command(queue)
    session = BlockingSession()
    worker = RunWorker(queue, session, lease_seconds=1.2)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(session.entered.wait(), 3)
        async with lease_stores[1].engine.begin() as conn:
            await conn.execute(
                update(run_jobs)
                .where(run_jobs.c.id == job["id"])
                .values(locked_until=datetime(2000, 1, 1, tzinfo=UTC))
            )
        await asyncio.wait_for(session.cancelled.wait(), 3)
        await asyncio.wait_for(task, 3)
    finally:
        session.release.set()
        await task
    assert await RunQueue(lease_stores[1].engine).claim("other") is None
    assert (await queue.get(job["id"], "actor-1"))["status"] == "recovery_required"


@pytest.mark.parametrize("failure", ["unavailable", "timeout"])
async def test_failed_heartbeat_stops_local_work_without_requeue(
    lease_stores, monkeypatch, failure
):
    queue = RunQueue(lease_stores[0].engine)
    job = await command(queue)
    session = BlockingSession()

    async def failed_renew(*args, **kwargs):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise OSError("injected renewal failure")

    monkeypatch.setattr(queue, "renew", failed_renew)
    worker = RunWorker(queue, session)
    heartbeat = worker._heartbeat

    async def heartbeat_after_start(claim):
        # Keep the ordinary DB claim/start deadline. This test injects a short
        # renewal deadline only after real execution has crossed its barrier.
        await session.entered.wait()
        worker.lease_seconds = 1.2
        await heartbeat(claim)

    monkeypatch.setattr(worker, "_heartbeat", heartbeat_after_start)
    await asyncio.wait_for(worker.run_once(), 15)
    assert session.cancelled.is_set() and session.calls == 1
    assert (await queue.get(job["id"], "actor-1"))["status"] == "running"
    assert not await RunWorker(
        RunQueue(lease_stores[1].engine), BlockingSession()
    ).run_once()


async def test_shutdown_settles_active_command_and_keeps_unclaimed_work(lease_stores):
    queue = RunQueue(lease_stores[0].engine)
    job = await command(queue)
    await add_report(lease_stores[0], "next")
    pending = await command(queue, report="next")
    session = BlockingSession()
    worker = RunWorker(queue, session, max_local=1, poll_interval=0.01)
    await worker.start()
    await asyncio.wait_for(session.entered.wait(), 3)
    await worker.aclose(grace_seconds=0.01, cancellation_seconds=2)
    assert session.stopping and session.cancelled.is_set()
    assert (await queue.get(job["id"], "actor-1"))["status"] == "failed"
    assert (await queue.get(pending["id"], "actor-1"))["status"] == "pending"
    with pytest.raises(RuntimeError, match="closed"):
        await worker.start()


async def test_shutdown_waits_for_real_request_ledger_settlement(lease_stores):
    from pydantic import Field
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.runtime.session import SessionRecord
    from tests.unit.test_orchestration import ScriptModel

    store = lease_stores[0]
    await store.create(
        SessionRecord(
            "metered", "actor-1", "v2", "policy-1", "metered", user_request="research"
        ),
        TokenBudget(limit=100000, budget_id="metered").snapshot(),
    )
    entered = asyncio.Event()

    class BlockingModel(ScriptModel):
        entered: object = Field(exclude=True)

        async def _agenerate(self, messages, **kwargs):
            self.entered.set()
            await asyncio.Event().wait()

    class Graph:
        async def ainvoke(self, value, config, *, context):
            model = MeteredChatModel.wrap(
                BlockingModel(script=[], entered=entered),
                budget=await store.load_budget(context),
                max_input_tokens=5000,
                max_output_tokens=100,
            )
            await model.ainvoke("local synthetic request")

    session = SessionService(
        store=store, agent_factory=lambda *_: Graph(), policy_version="policy-1"
    )
    queue = RunQueue(store.engine)
    job = await command(
        queue, report="metered", kind="start", payload={"message": "research"}
    )
    worker = RunWorker(queue, session)
    await worker.start()
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        await worker.aclose(grace_seconds=0.01, cancellation_seconds=3)
    record = await store.load("metered", "actor-1")
    assert record.status == "failed" and not record.cancel_requested
    budget = await store.load_budget(session._context(record))
    assert budget.unknown_requests == 1 and not budget.snapshot()["pending"]
    assert (await queue.get(job["id"], "actor-1"))["status"] == "failed"


async def test_shutdown_timeout_keeps_handle_and_lease_until_settlement(lease_stores):
    class SlowSettlement(BlockingSession):
        async def execute_command(self, job, *, before_execute):
            await before_execute()
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()

    queue = RunQueue(lease_stores[0].engine)
    job = await command(queue)
    session = SlowSettlement()
    worker = RunWorker(queue, session, lease_seconds=1.2)
    await worker.start()
    await asyncio.wait_for(session.entered.wait(), 3)
    try:
        with pytest.raises(TimeoutError, match="shutdown"):
            await worker.aclose(grace_seconds=0.01, cancellation_seconds=0.01)
        assert worker._tasks
        await asyncio.sleep(1.6)
        assert await RunQueue(lease_stores[1].engine).claim("follower") is None
        assert (await queue.get(job["id"], "actor-1"))["status"] == "running"
    finally:
        session.release.set()
        await worker.drain()
    assert (await queue.get(job["id"], "actor-1"))["status"] == "failed"


async def test_actual_two_process_workers_do_not_resend_killed_model_request(
    lease_stores, tmp_path
):
    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.runtime.session import SessionRecord
    from research_copilot.runtime.storage import postgres_runtime

    store = lease_stores[0]
    if store.engine.dialect.name != "postgresql":
        pytest.skip("Two process execution requires isolated PostgreSQL")
    url = store.engine.url.render_as_string(hide_password=False)
    async with postgres_runtime(url, initialize=True):
        pass
    await store.create(
        SessionRecord(
            "metered", "actor-1", "v2", "policy-1", "metered", user_request="research"
        ),
        TokenBudget(limit=100000, budget_id="metered").snapshot(),
    )
    queue = RunQueue(store.engine)
    job = await command(
        queue, report="metered", kind="start", payload={"message": "research"}
    )
    children, logs = [], []
    roots = [tmp_path / "owner", tmp_path / "follower"]

    async def spawn(root):
        root.mkdir()
        log = (root / "worker.log").open("w", encoding="utf-8")
        logs.append(log)
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.fakes.run_worker_process", str(root)],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "RC_TEST_DATABASE_URL": url},
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
        async with asyncio.timeout(8):
            while not (roots[0] / "called").exists():
                assert owner.poll() is None
                await asyncio.sleep(0.05)
        follower = await spawn(roots[1])
        await asyncio.sleep(2.6)
        assert owner.poll() is None and follower.poll() is None
        assert not (roots[1] / "called").exists()
        owner.kill()
        owner.wait(timeout=5)
        async with asyncio.timeout(8):
            while (await queue.get(job["id"], "actor-1"))[
                "status"
            ] != "recovery_required":
                assert follower.poll() is None
                await asyncio.sleep(0.05)
        assert not (roots[1] / "called").exists()
        record = await store.load("metered", "actor-1")
        from research_copilot.contracts import RunContext

        budget = await store.load_budget(
            RunContext(
                record.actor_id,
                record.report_id,
                record.run_id,
                record.budget_id,
                record.policy_version,
            )
        )
        assert len(budget.snapshot()["pending"]) == 1
        request_id = next(iter(budget.snapshot()["pending"]))
        assert len(budget.requests) == 1
        assert budget.requests[request_id]["sent"] is True
        assert "usage" not in budget.requests[request_id]
        assert (await queue.get(job["id"], "actor-1"))["started_at"] is not None
        from research_copilot.runtime.request_recovery import recover_request
        from research_copilot.runtime.run_recovery import RunRecovery
        from research_copilot.runtime.run_queue import QueueConflict

        lost = await queue.get(job["id"], "actor-1")
        release = dict(
            report_id="metered",
            actor_id="actor-1",
            job_id=job["id"],
            claim_token=lost["claim_token"],
            owner_stopped=True,
            note="Isolated OS owner killed and awaited; fake model has no external execution",
        )
        with pytest.raises(QueueConflict, match="pending"):
            await RunRecovery(lease_stores[1]).release(**release)
        recovered = await recover_request(
            store,
            report_id="metered",
            actor_id="actor-1",
            request_id=request_id,
            note="Known sent request; usage unavailable",
        )
        assert recovered["after"]["unknown_requests"] == 1
        receipt = await RunRecovery(lease_stores[1]).release(**release)
        assert receipt["unresolved_requests"] == [request_id]
        assert (await queue.get(job["id"], "actor-1"))["status"] == "failed"
        await asyncio.sleep(0.5)
        assert follower.poll() is None and not (roots[1] / "called").exists()
        current_budget = await store.load_budget(
            RunContext(
                record.actor_id,
                record.report_id,
                record.run_id,
                record.budget_id,
                record.policy_version,
            )
        )
        assert current_budget.status() == recovered["after"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        for log in logs:
            log.close()
