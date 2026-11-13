"""Experience API: report retrospective view, disable toggling and workspace summary."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import RunContext
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.experience import (
    ExperienceService,
    enqueue_delivery,
    experiences,
)
from research_copilot.runtime.session import SessionRecord, SessionStatus
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.web.app import WorkbenchBackend, create_app


class IdleSession:
    async def get_status(self, report_id, actor_id):
        return SessionStatus(report_id, "idle", ())


async def _create_report(store, report_id, actor_id="actor-1"):
    budget_id = "budget-1" if report_id == "report-1" else report_id
    await store.create(
        SessionRecord(
            report_id,
            actor_id,
            "v2",
            "policy-1",
            budget_id,
            run_id="run-" + report_id,
            status="idle",
        ),
        TokenBudget(limit=100000, budget_id=budget_id).snapshot(),
    )


@pytest.mark.asyncio
async def test_report_experience_view_and_disable_endpoint(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    try:
        await _create_report(store, "report-1")
        await _create_report(store, "source-report")
        now = datetime.now(UTC)
        rows = [
            {
                "id": uuid4().hex,
                "source_report_id": "source-report",
                "source_report_version": "1",
                "source_job_id": "job-1",
                "role": "writer",
                "stage": "writing",
                "problem_type": "numeric_accuracy",
                "applies_when": "审阅退回指出数字单位或期间不一致时。",
                "recommendation": "先冻结口径与单位，再逐项对照原证据。",
                "evidence_refs": ["review:source-report/review-1/issue:issue-1"],
                "enabled": True,
                "disabled_at": None,
                "disabled_by": None,
                "fingerprint": hashlib.sha256(b"api").hexdigest(),
                "created_at": now,
            }
        ]
        async with engine.begin() as connection:
            await connection.execute(experiences.insert(), rows)
            await enqueue_delivery(
                connection,
                report_id="report-1",
                report_version="1",
                report_artifact_id="report-1",
                report_content_hash="c" * 64,
                actor_id="actor-1",
            )
        experience_id = rows[0]["id"]
        context = RunContext(
            "actor-1", "report-1", "run-report-1", "budget-1", "policy-1"
        )

        def recall(state):
            return None

        await store.mutate_control(
            context,
            recall,
            effects=[
                {
                    "kind": "experience_recall",
                    "report_id": "report-1",
                    "dispatch_id": "dispatch-1",
                    "role": "writer",
                    "stage": "writing",
                    "experience_ids": [experience_id],
                }
            ],
        )
        backend = WorkbenchBackend(
            session=IdleSession(),
            store=store,
            actor_id="actor-1",
            workspace_root=tmp_path / "workspace",
            experience=ExperienceService(store),
        )
        app = create_app(backend=backend)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost"
        ) as client:
            response = await client.get("/api/reports/report-1/experiences")
            assert response.status_code == 200
            body = response.json()
            assert body["received"][0]["experience_id"] == experience_id
            assert body["received"][0]["usage"]["adopted"] is False
            assert body["jobs"][0]["status"] == "pending"

            workspace = await client.get("/api/reports/report-1")
            assert workspace.status_code == 200
            assert workspace.json()["retrospective"]["status"] == "pending"
            assert workspace.json()["retrospective"]["job_count"] == 1

            disabled = await client.patch(
                f"/api/experiences/{experience_id}", json={"enabled": False}
            )
            assert disabled.status_code == 200
            assert disabled.json()["experience"]["enabled"] is False
            assert await ExperienceService(store).recall("writer", "writing") == []

            enabled = await client.patch(
                f"/api/experiences/{experience_id}", json={"enabled": True}
            )
            assert enabled.status_code == 200
            assert len(await ExperienceService(store).recall("writer", "writing")) == 1

            invalid = await client.patch(
                f"/api/experiences/{experience_id}", json={"enabled": "no"}
            )
            assert invalid.status_code == 400
            missing = await client.patch(
                "/api/experiences/unknown", json={"enabled": False}
            )
            assert missing.status_code == 404
    finally:
        await engine.dispose()
