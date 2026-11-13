"""PostgreSQL acceptance: durable checkpoints, restart recovery and job exclusivity.

Run explicitly::

    RC_TEST_DATABASE_URL=postgresql://... pytest -q -m db tests/integration/test_experience_postgres.py

These tests use the configured local development database and clean their own
rows. They are skipped by the default offline suite.
"""

import asyncio
import json
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef, RunContext
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.experience import (
    ExperienceDraft,
    StaleClaim,
    enqueue_delivery,
    experiences,
    experience_usages,
    retrospective_jobs,
)
from research_copilot.runtime.identity import checkpoint_thread_id
from research_copilot.runtime.session import SessionRecord, SessionService
from research_copilot.runtime.storage import SqlSessionStore, sessions
from research_copilot.runtime.urls import to_sqlalchemy_url

pytestmark = pytest.mark.db


def _url():
    url = os.environ.get("RC_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("需要 RC_TEST_DATABASE_URL 指向本地 PostgreSQL")
    return url


async def _cleanup(engine, report_id):
    async with engine.begin() as connection:
        await connection.execute(
            delete(experience_usages).where(experience_usages.c.report_id == report_id)
        )
        await connection.execute(
            delete(experiences).where(experiences.c.source_report_id == report_id)
        )
        await connection.execute(
            delete(retrospective_jobs).where(
                retrospective_jobs.c.report_id == report_id
            )
        )
        await connection.execute(
            delete(sessions).where(sessions.c.report_id == report_id)
        )


@pytest.mark.asyncio
async def test_postgres_job_claim_is_exclusive_and_completion_is_transactional():
    from research_copilot.runtime.storage import SqlSessionStore

    url = _url()
    report_id = "pg-exp-" + uuid4().hex
    engine1 = create_async_engine(to_sqlalchemy_url(url))
    engine2 = create_async_engine(to_sqlalchemy_url(url))
    store1 = SqlSessionStore(engine1)
    store2 = SqlSessionStore(engine2)
    try:
        await store1.setup()
        await store1.create(
            SessionRecord(
                report_id, "actor-1", "v2", "policy-1", report_id, run_id="run-1"
            ),
            TokenBudget(limit=1000, budget_id=report_id).snapshot(),
        )
        async with engine1.begin() as connection:
            await enqueue_delivery(
                connection,
                report_id=report_id,
                report_version="1",
                report_artifact_id="report-1",
                report_content_hash="c" * 64,
                actor_id="actor-1",
            )
            await enqueue_delivery(
                connection,
                report_id=report_id,
                report_version="1",
                report_artifact_id="report-1",
                report_content_hash="c" * 64,
                actor_id="actor-1",
            )
        assert len(await store1.experience.jobs(report_id)) == 1

        claims = await asyncio.gather(
            store1.experience.claim(lease_seconds=30),
            store2.experience.claim(lease_seconds=30),
        )
        claims = [claim for claim in claims if claim is not None]
        assert len(claims) == 1
        claim = claims[0]

        with pytest.raises(StaleClaim):
            await store2.experience.complete(
                {**claim, "claim_token": "bogus"},
                [
                    {
                        "role": "writer",
                        "stage": "writing",
                        "problem_type": "numeric_accuracy",
                        "applies_when": "存在未解决的数字口径问题时。",
                        "recommendation": "先统一口径与单位，再按原证据逐项重算。",
                        "evidence_refs": ["review:r/issue:i"],
                        "fingerprint": "f" * 64,
                    }
                ],
            )
        assert (await store1.experience.jobs(report_id))[0]["status"] == "running"

        draft = ExperienceDraft(
            role="writer",
            stage="writing",
            problem_type="numeric_accuracy",
            applies_when="存在未解决的数字口径问题时。",
            recommendation="先统一口径与单位，再按原证据逐项重算。",
            evidence_refs=["review:r/issue:i"],
        )
        from research_copilot.runtime.experience import normalize_drafts

        normalized = normalize_drafts([draft], {"review:r/issue:i"})
        assert (
            await store1.experience.complete(claim, normalized, usage={"tokens": 3})
            == 1
        )
        assert (await store1.experience.jobs(report_id))[0]["status"] == "completed"
        with pytest.raises(StaleClaim):
            await store1.experience.complete(claim, normalized)
        view = await store1.experience.report_view(report_id)
        assert len(view["produced"]) == 1
    finally:
        await _cleanup(engine1, report_id)
        await engine1.dispose()
        await engine2.dispose()


@pytest.mark.asyncio
async def test_postgres_restart_reclaims_running_job_once():
    url = _url()
    report_id = "pg-exp-" + uuid4().hex
    engine1 = create_async_engine(to_sqlalchemy_url(url))
    engine2 = create_async_engine(to_sqlalchemy_url(url))
    try:
        store1 = SqlSessionStore(engine1)
        await store1.setup()
        await store1.create(
            SessionRecord(
                report_id, "actor-1", "v2", "policy-1", report_id, run_id="run-1"
            ),
            TokenBudget(limit=1000, budget_id=report_id).snapshot(),
        )
        async with engine1.begin() as connection:
            await enqueue_delivery(
                connection,
                report_id=report_id,
                report_version="1",
                report_artifact_id="report-1",
                report_content_hash="c" * 64,
                actor_id="actor-1",
            )
        claim = await store1.experience.claim(lease_seconds=30)
        assert claim is not None
        await engine1.dispose()  # process exit after leasing, before completion

        store2 = SqlSessionStore(engine2)
        assert await store2.experience.claim() is None
        async with engine1.begin() as connection:
            await connection.execute(
                retrospective_jobs.update()
                .where(retrospective_jobs.c.id == claim["id"])
                .values(locked_until=datetime(2000, 1, 1, tzinfo=UTC))
            )
        reclaimed = await store2.experience.claim(lease_seconds=30)
        assert reclaimed is not None
        assert reclaimed["id"] == claim["id"]
        assert reclaimed["attempts"] == 2

        from research_copilot.runtime.experience import normalize_drafts

        normalized = normalize_drafts(
            [
                ExperienceDraft(
                    role="analysist",
                    stage="analysis",
                    problem_type="valuation_method",
                    applies_when="估值假设缺少敏感性分析时。",
                    recommendation="补充 WACC 与永续增长率的双因素敏感性表。",
                    evidence_refs=["review:r/issue:i"],
                )
            ],
            {"review:r/issue:i"},
        )
        await store2.experience.complete(reclaimed, normalized)
        jobs = await store2.experience.jobs(report_id)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "completed"
        assert jobs[0]["attempts"] == 2
        view = await store2.experience.report_view(report_id)
        assert len(view["produced"]) == 1
    finally:
        await _cleanup(engine1, report_id)
        await _cleanup(engine2, report_id)
        await engine1.dispose()
        await engine2.dispose()


@pytest.mark.asyncio
async def test_postgres_checkpoint_budget_and_duplicate_resume():
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import interrupt

    from research_copilot.runtime.checkpoint import checkpoint_serializer
    from research_copilot.runtime.urls import to_checkpoint_url

    url = _url()
    report_id = "pg-exp-" + uuid4().hex
    engine = create_async_engine(to_sqlalchemy_url(url))
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord(report_id, "actor-1", "v2", "policy-1", report_id),
        TokenBudget(limit=1000, budget_id=report_id).snapshot(),
    )
    release = asyncio.Event()
    executions = {"gate": 0, "finish": 0}

    def build(agent_saver):
        async def gate(state):
            executions["gate"] += 1
            answer = interrupt(
                {
                    "action_requests": [
                        {"name": "confirm_plan", "args": {"artifact": "x"}}
                    ]
                }
            )
            await release.wait()
            return {"messages": [AIMessage(content=json.dumps(answer))]}

        async def finish(state):
            executions["finish"] += 1
            return {}

        graph = StateGraph(MessagesState, context_schema=RunContext)
        graph.add_node("gate", gate)
        graph.add_node("finish", finish)
        graph.add_edge(START, "gate")
        graph.add_edge("gate", "finish")
        graph.add_edge("finish", END)
        return graph.compile(checkpointer=agent_saver)

    async with AsyncPostgresSaver.from_conn_string(
        to_checkpoint_url(url), serde=checkpoint_serializer()
    ) as saver:
        await saver.setup()
        first = SessionService(
            store=store,
            agent_factory=lambda record, control: build(saver),
            policy_version="policy-1",
        )
        status = await first.start(report_id, "actor-1", "research")
        assert status.status == "paused"
        record = await store.load(report_id, "actor-1")
        context = RunContext(
            record.actor_id,
            record.report_id,
            record.run_id,
            record.budget_id,
            record.policy_version,
        )
        budget = await store.load_budget(context)
        reservation = await budget.reserve(123)

        async def validator(context, pending, decisions):
            assert pending and decisions

        store.resume_validator = validator
        pending = {item["id"]: item["value"] for item in status.interrupts}
        decision = {next(iter(pending)): {"decisions": [{"type": "approve"}]}}
        resumed = asyncio.create_task(first.resume(report_id, "actor-1", decision))
        for _ in range(50):
            if executions["gate"] == 2:
                break
            await asyncio.sleep(0.02)
        assert executions["gate"] == 2
        with pytest.raises(Exception, match="already running|busy"):
            await first.resume(report_id, "actor-1", decision)
        release.set()
        result = await resumed
        assert result.status == "idle"
        assert executions["finish"] == 1

        # Reopen the checkpoint and budget exactly as a restarted service would.
        reloaded = SqlSessionStore(engine)
        restored_budget = await reloaded.load_budget(context)
        assert reservation in restored_budget.snapshot()["pending"]
        restarted = SessionService(
            store=reloaded,
            agent_factory=lambda record, control: build(saver),
            policy_version="policy-1",
        )
        snapshot = await restarted.get_status(report_id, "actor-1")
        assert snapshot.status == "idle"
        # No pending interrupt: a repeated resume must not execute the graph again.
        before = dict(executions)
        with pytest.raises(ValueError):
            await restarted.resume(report_id, "actor-1", decision)
        assert executions == before
        if hasattr(saver, "adelete_thread"):
            await saver.adelete_thread(checkpoint_thread_id(report_id))
    await _cleanup(engine, report_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_signoff_enqueues_in_the_approval_transaction():
    from research_copilot.runtime.business import BusinessCoordinator, approval_key

    url = _url()
    report_id = "pg-exp-" + uuid4().hex
    engine = create_async_engine(to_sqlalchemy_url(url))
    store = SqlSessionStore(engine)
    await store.setup()
    try:
        await store.create(
            SessionRecord(
                report_id, "actor-1", "v2", "policy-1", report_id, run_id="run-1"
            ),
            TokenBudget(limit=1000, budget_id=report_id).snapshot(),
        )
        plan = ArtifactRef(artifact_id="plan:one", version="1", content_hash="a" * 64)
        analysis = ArtifactRef(
            artifact_id="analysis-1", version="1", content_hash="b" * 64
        )
        report = ArtifactRef(artifact_id="report-1", version="1", content_hash="c" * 64)
        context = RunContext("actor-1", report_id, "run-1", report_id, "policy-1")

        def seed(state):
            state.artifacts["plan"] = plan
            state.approved["plan"] = plan
            state.artifacts["analysis"] = analysis
            state.approved["analysis"] = analysis
            state.artifacts["report"] = report
            state.reviewed_report = report
            state.role_data["reviews"] = {
                "review-1": {
                    "review_id": "review-1",
                    "reviewed": report.model_dump(),
                    "outcome": "passed",
                    "checks_complete": True,
                    "pass_confidence": 0.95,
                    "issues": [],
                    "decision_source": "automatic",
                }
            }
            state.role_data["objects"] = {
                report.artifact_id: {
                    "kind": "report",
                    "ref": report.model_dump(),
                    "payload": {"analysis_ref": analysis.model_dump()},
                }
            }
            state.grants[approval_key(report_id, "signoff_report", report)] = "actor-1"

        await store.mutate_control(context, seed)
        business = BusinessCoordinator(store)
        key = approval_key(report_id, "signoff_report", report)
        first = await business.confirm(context, "signoff_report", report, key)
        second = await business.confirm(context, "signoff_report", report, key)
        assert first == second
        jobs = await store.experience.jobs(report_id)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "pending"
        assert jobs[0]["report_version"] == "1"
    finally:
        await _cleanup(engine, report_id)
        await engine.dispose()
