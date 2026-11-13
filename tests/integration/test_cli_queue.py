"""CLI workers consume only their command while sharing database admission."""

import asyncio
import pytest
from sqlalchemy import update

from research_copilot.runtime.run_queue import RunQueue, run_queue_settings
from research_copilot.runtime.session import SessionStatus
from tests.integration import test_retrospective_leases as lease_fixtures
from tests.integration.test_run_queue import add_report, enqueue

lease_stores = lease_fixtures.lease_stores


class LocalSession:
    def __init__(self):
        self.calls = []
        self.stopped = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def stop_runs(self):
        self.stopped = True
        self.release.set()

    async def execute_command(self, job, *, before_execute):
        await before_execute()
        self.calls.append(job["id"])
        self.entered.set()
        await self.release.wait()

    async def get_status(self, report_id, actor_id):
        return SessionStatus(report_id, "idle")


async def test_cli_target_does_not_consume_older_unrelated_command(lease_stores):
    from research_copilot.runtime.local_runner import run_command

    queue = RunQueue(lease_stores[0].engine)
    unrelated = await enqueue(queue)
    await add_report(lease_stores[0], "cli-report")
    target = await enqueue(queue, "cli-report")
    session = LocalSession()
    result = await run_command(queue, session, target)
    assert result.report_id == "cli-report"
    assert session.calls == [target["id"]] and session.stopped
    assert (await queue.get(unrelated["id"], "actor-1"))["status"] == "pending"


async def test_cli_waits_for_global_slot_and_shutdown_leaves_waiting_command(
    lease_stores,
):
    from research_copilot.runtime.local_runner import run_command

    store = lease_stores[0]
    queue = RunQueue(store.engine)
    async with store.engine.begin() as connection:
        await connection.execute(update(run_queue_settings).values(max_active=1))
    await enqueue(queue)
    occupied = await queue.claim("other-worker")
    await queue.start(occupied)
    await add_report(store, "cli-report")
    target = await enqueue(queue, "cli-report")
    session = LocalSession()
    running = asyncio.create_task(run_command(queue, session, target))
    await asyncio.sleep(0.3)
    assert not session.calls and not running.done()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert session.stopped
    assert (await queue.get(target["id"], "actor-1"))["status"] == "pending"
    await queue.finish(occupied, succeeded=True)
    resumed = LocalSession()
    await run_command(queue, resumed, target)
    assert resumed.calls == [target["id"]]


async def test_cli_cancellation_waits_for_running_command_to_settle(lease_stores):
    from research_copilot.runtime.local_runner import run_command

    queue = RunQueue(lease_stores[0].engine)
    job = await enqueue(queue)
    session = LocalSession()
    session.release.clear()
    running = asyncio.create_task(run_command(queue, session, job))
    await asyncio.wait_for(session.entered.wait(), 3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert session.stopped
    assert (await queue.get(job["id"], "actor-1"))["status"] == "succeeded"


async def test_cli_entrypoints_persist_commands_and_cancel_waiting(
    lease_stores, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from sqlalchemy.ext.asyncio import create_async_engine
    from typer.testing import CliRunner
    from research_copilot import cli, host
    from research_copilot.runtime.storage import SqlSessionStore

    url = lease_stores[0].engine.url.render_as_string(hide_password=False)
    queue = RunQueue(lease_stores[0].engine)
    waiting = await enqueue(queue)
    calls = []

    @asynccontextmanager
    async def runtime(*args, **kwargs):
        engine = create_async_engine(url)
        try:
            yield SqlSessionStore(engine), None
        finally:
            await engine.dispose()

    @asynccontextmanager
    async def host_session(*args, **kwargs):
        async with runtime() as (store, _):
            session = LocalSession()
            session.calls = calls
            yield session, store, None

    monkeypatch.setattr(
        cli, "_settings_or_exit", lambda: SimpleNamespace(database_url=url)
    )
    monkeypatch.setattr(host, "open_session", host_session)
    monkeypatch.setattr("research_copilot.runtime.storage.postgres_runtime", runtime)
    runner = CliRunner()
    options = [
        "--report-id",
        "cli-report",
        "--actor-id",
        "actor-1",
        "--models",
        "offline.toml",
        "--policy-version",
        "policy-1",
        "--workspace",
        str(tmp_path),
    ]
    started = await asyncio.to_thread(
        runner.invoke, cli.app, ["start", *options, "--message", "offline"]
    )
    assert started.exit_code == 0, started.output
    latest = await queue.latest("cli-report", "actor-1")
    assert latest["kind"] == "start" and latest["status"] == "succeeded"
    assert calls == [latest["id"]]
    resumed = await asyncio.to_thread(
        runner.invoke, cli.app, ["resume", *options, "--operation-id", "resume-cli-1"]
    )
    assert resumed.exit_code == 0, resumed.output
    assert (await queue.latest("cli-report", "actor-1"))["kind"] == "resume"
    assert len(calls) == 2
    repeated = await asyncio.to_thread(
        runner.invoke, cli.app, ["resume", *options, "--operation-id", "resume-cli-1"]
    )
    assert repeated.exit_code == 0 and len(calls) == 2
    assert (await queue.get(waiting["id"], "actor-1"))["status"] == "pending"
    cancelled = await asyncio.to_thread(
        runner.invoke,
        cli.app,
        ["cancel", "--report-id", "report-1", "--actor-id", "actor-1"],
    )
    assert cancelled.exit_code == 0, cancelled.output
    assert (await queue.get(waiting["id"], "actor-1"))["status"] == "cancelled"
    import json

    assert json.loads(cancelled.output)["command"]["status"] == "cancelled"


async def test_local_runner_preserves_real_session_checkpoint_approval(lease_stores):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END, MessagesState
    from langgraph.types import interrupt
    from research_copilot.runtime.run_admission import create_start
    from research_copilot.runtime.local_runner import run_command
    from research_copilot.runtime.session import SessionService, SessionRecord
    from research_copilot.runtime.budget import TokenBudget

    store = lease_stores[0]
    decisions = []

    # SessionStore.validate_resume delegates a RunContext and two payloads.
    async def business_validate(context, pending, supplied):
        decisions.append(supplied)

    store.resume_validator = business_validate

    def gate(state):
        interrupt({"action_requests": [{"name": "confirm_plan", "args": {}}]})
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("gate", gate)
    graph.add_edge(START, "gate")
    graph.add_edge("gate", END)
    compiled = graph.compile(checkpointer=InMemorySaver())

    def session():
        return SessionService(
            store=store, agent_factory=lambda *_: compiled, policy_version="policy-1"
        )

    report_id = "cli-" + "a" * 124
    _, job = await create_start(
        store,
        SessionRecord(
            report_id, "actor-1", "v2", "policy-1", report_id, user_request="offline"
        ),
        TokenBudget(limit=10000, budget_id=report_id).snapshot(),
    )
    queue = RunQueue(store.engine)
    paused = await run_command(queue, session(), job)
    assert paused.status == "paused" and len(paused.interrupts) == 1
    original = await store.load(report_id, "actor-1")
    supplied = {paused.interrupts[0]["id"]: {"decisions": [{"type": "approve"}]}}
    next_job = await queue.enqueue(
        report_id=report_id,
        actor_id="actor-1",
        operation_id="resume-real",
        kind="resume",
        payload={"decision": supplied},
    )
    await run_command(queue, session(), next_job)
    final = await store.load(report_id, "actor-1")
    assert final.run_id == original.run_id and final.budget_id == original.budget_id
    assert decisions == [supplied]
