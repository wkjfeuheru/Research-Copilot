"""Real Web/Session/product graph integration from a prepared draft; fake models."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import URL
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
import uvicorn

from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.business import BusinessCoordinator, ResearchPlan
from research_copilot.runtime.experience import ExperienceService
from research_copilot.runtime.session import SessionService
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.urls import to_checkpoint_url
from research_copilot.subagents.services import RoleServices
from research_copilot.web.app import WorkbenchBackend, create_app
from research_copilot.workspace import bundled_resources
from tests.integration.support import RespondingModel
from tests.integration.test_reviewer import submitted_draft
from tests.unit.test_orchestration import ScriptModel, call, context


async def prepare_review_backend(env, root, saver):
    services, business, store = env
    services.experience = ExperienceService(store)

    def respond(messages, step):
        payload = json.loads(
            next(m.content for m in messages if isinstance(m, HumanMessage))
        )
        response = call(
            "BranchReview",
            {
                "pass_confidence": 0.99,
                "checks_complete": True,
                "covered_targets": [
                    target["target_id"] for target in payload["targets"]
                ],
                "issues": [
                    {
                        "category": "reasoning",
                        "severity": "major",
                        "target_id": "document:0",
                        "location": "Revenue",
                        "quote": "Source-supported",
                        "basis": "E-000001",
                        "reason": "Check scope",
                        "suggestion": "Clarify claim",
                    }
                ],
            },
            f"offline-review-{step}",
        )
        response.usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        }
        return response

    models = {
        name: ScriptModel(script=[AIMessage(content="must remain unused")])
        for name in ("supervisor", "searcher", "analysist", "writer")
    }
    models["reviewer"] = RespondingModel(script=[], respond=respond)
    factory = SupervisorFactory(
        models=models,
        store=store,
        business=business,
        checkpointer=saver,
        workspace_root=services.workspace_root,
        resources_root=bundled_resources(),
        role_services=services,
    )
    from research_copilot.runtime.control import RunControl

    await store.mutate_control(
        context(), lambda s: s.role_data.update(workflow_version="products-v2")
    )
    await factory(await store.load("report-1", "actor-1"), RunControl())
    await store.mutate_control(
        context(), lambda s: s.role_data.update(workflow_version="products-v1")
    )
    await submitted_draft(env)
    await store.mutate_control(
        context(), lambda s: s.role_data.update(workflow_version="products-v2")
    )
    from research_copilot.runtime.autonomy import ReportScope

    await business.set_report_scope(
        context(),
        ReportScope(
            report_type="company_report",
            objective="Review prepared draft",
            as_of="2025-04-30",
        ),
    )
    from tests.integration.support import tool_outputs

    def supervise(messages, step):
        if step == 0:
            response = call(
                "task",
                {
                    "subagent_type": "reviewer",
                    "description": json.dumps(
                        {
                            "task_id": "review-native",
                            "role": "reviewer",
                            "objective": "Review current report",
                            "as_of": "2025-04-30",
                        }
                    ),
                },
                "native-review",
            )
        elif step == 1:
            response = call(
                "signoff_report",
                {"artifact": tool_outputs(messages)["task"]["reviewed"]},
                "native-signoff",
            )
        else:
            response = AIMessage(content="Report signed.")
        response.usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        }
        return response

    models["supervisor"] = RespondingModel(script=[], respond=supervise)
    store.resume_validator = business.validate_resume
    session = SessionService(
        store=store, agent_factory=factory, policy_version="policy-1"
    )
    backend = WorkbenchBackend(
        session=session,
        store=store,
        actor_id="actor-1",
        workspace_root=services.workspace_root,
        experience=services.experience,
        policy_version="policy-1",
        execution_policy=Path(__file__).resolve().parents[2]
        / "config/execution_policy.toml",
    )
    record = await store.load("report-1", "actor-1")
    initial = await session._invoke(
        record,
        {
            "messages": [
                {"role": "user", "content": "离线联合验收夹具：审核并签署预制草稿"}
            ]
        },
    )
    assert initial.status == "paused" and initial.interrupts
    return backend, models


@asynccontextmanager
async def serve_test_app(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(
            uvicorn.Config(
                app, log_level="error", lifespan="off", timeout_graceful_shutdown=2
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.db)])
async def web_review_environment(request, role_env, tmp_path):
    if request.param == "sqlite":
        yield role_env, InMemorySaver()
        return
    base = os.environ.get("RC_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Requires explicit isolated RC_TEST_DATABASE_URL")
    base = to_checkpoint_url(base)
    params = psycopg.conninfo.conninfo_to_dict(base)
    name = "rc_webtest_" + uuid4().hex
    url = URL.create(
        "postgresql",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", 5432)),
        database=name,
    ).render_as_string(hide_password=False)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            async with postgres_runtime(url, initialize=True) as (store, saver):
                source = role_env[2]
                await store.create(
                    await source.load("report-1", "actor-1"),
                    (await source.load_budget(context())).snapshot(),
                    workflow_version="products-v1",
                )
                business = BusinessCoordinator(store)
                state = await source.read_control(context())
                plan = await business.propose_plan(
                    context(), ResearchPlan.model_validate(state.plans[0]["plan"])
                )
                await store.mutate_control(
                    context(), lambda current: current.approved.update(plan=plan)
                )
                services = RoleServices(
                    repository=store,
                    workspace_root=tmp_path / "pg-workspace",
                    business=business,
                    report_rules=role_env[0].report_rules,
                )
                yield (services, business, store), saver
        finally:
            assert name.startswith("rc_webtest_") and name != params.get("dbname")
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


async def test_web_review_dispositions_signoff_and_sse_share_real_session(
    web_review_environment, tmp_path
):
    role_env, saver = web_review_environment
    backend, models = await prepare_review_backend(role_env, tmp_path, saver)
    store = role_env[2]
    try:
        async with serve_test_app(create_app(backend=backend)) as url:
            async with httpx.AsyncClient(base_url=url, timeout=10) as client:
                status = (await client.get("/api/reports/report-1")).json()
                gate = status["interrupts"][0]
                assert gate["kind"] == "reviewer_deep_review"
                review = gate["review"]
                assert review["checks_complete"] and review["issues"]
                assert not status["signoff_ready"] and not status["signed"]
                decision = {
                    "decision": "approve",
                    "comment": "",
                    "issue_dispositions": [],
                    "snapshot_id": review["snapshot_id"],
                    "review_session_id": review["review_session_id"],
                }
                calls = models["reviewer"].position
                response = await client.post(
                    "/api/reports/report-1/decisions",
                    json={"decisions": {gate["id"]: decision}},
                )
                assert response.status_code == 200
                await backend.runs.drain()
                status = (await client.get("/api/reports/report-1")).json()
                retry = status["interrupts"][0]
                assert retry["review"]["errors"] and not status["signoff_ready"]
                assert models["reviewer"].position == calls
                decision["issue_dispositions"] = [
                    {
                        "issue_id": issue["issue_id"],
                        "decision": "false_positive",
                        "reason": "Verified source and frozen limitations in this offline fixture",
                    }
                    for issue in retry["review"]["issues"]
                ]
                response = await client.post(
                    "/api/reports/report-1/decisions",
                    json={"decisions": {retry["id"]: decision}},
                )
                assert response.status_code == 200
                await backend.runs.drain()
                status = (await client.get("/api/reports/report-1")).json()
                assert status["signoff_ready"] and not status["signed"], status
                signoff = status["interrupts"][0]
                assert signoff["tool"] == "signoff_report"
                response = await client.post(
                    "/api/reports/report-1/decisions",
                    json={
                        "decisions": {
                            signoff["id"]: {"decisions": [{"type": "approve"}]}
                        }
                    },
                )
                assert response.status_code == 200
                await backend.runs.drain()
                status = (await client.get("/api/reports/report-1")).json()
                assert status["signed"] and not status["interrupts"], status
                assert models["reviewer"].position == calls
                assert all(
                    models[name].position == 0
                    for name in ("searcher", "analysist", "writer")
                )
                assert models["supervisor"].position == 3
                before = (await store.read_control(context())).model_dump()
                duplicate = await client.post(
                    "/api/reports/report-1/decisions",
                    json={
                        "decisions": {
                            signoff["id"]: {"decisions": [{"type": "approve"}]}
                        }
                    },
                )
                assert duplicate.status_code == 400
                assert (await store.read_control(context())).model_dump() == before
                assert len(await store.experience.jobs("report-1")) == 1
                jobs = await client.get("/api/reports/report-1/experiences")
                assert jobs.status_code == 200 and len(jobs.json()["jobs"]) == 1
                chunks = []
                event = ""
                async with client.stream(
                    "GET", "/api/reports/report-1/events"
                ) as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:])
                            if event == "workspace":
                                assert data["signed"]
                            elif event == "report.delta":
                                chunks.append(data["text"])
                            elif event == "report.complete":
                                assert (
                                    hashlib.sha256("".join(chunks).encode()).hexdigest()
                                    == data["content_hash"]
                                )
                                break
                document = (await client.get("/api/reports/report-1/report")).json()
                assert document["is_frozen"] and document["content"] == "".join(chunks)
                offset = min(1024, len(document["content"]))
                resumed = []
                async with client.stream(
                    "GET",
                    "/api/reports/report-1/events",
                    headers={
                        "Last-Event-ID": f"report:{document['content_hash']}:{offset}"
                    },
                ) as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:])
                            if event == "report.start":
                                assert data["offset"] == offset
                            elif event == "report.delta":
                                resumed.append(data["text"])
                            elif event == "report.complete":
                                break
                assert "".join(resumed) == document["content"][offset:]
                assert models["reviewer"].position == calls
    finally:
        await backend.runs.aclose()
