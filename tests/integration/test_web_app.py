"""Local workbench API. The session is in-memory; no model is called."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef, RunContext
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import (
    SessionRecord,
    SessionStatus,
    validate_decisions,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "config" / "execution_policy.toml"
SECRET = "UNIQUESECRET123"


class FakeSession:
    def __init__(self, store):
        self.store = store
        self.pending: dict[str, tuple] = {}
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    def stop_runs(self):
        self.gate.set()

    async def execute_command(self, job, *, before_execute):
        await before_execute()
        payload = job["payload"]
        if job["kind"] == "start":
            return await self.start(
                job["report_id"], job["actor_id"], payload["message"]
            )
        if job["kind"] == "resume":
            return await self.resume(
                job["report_id"], job["actor_id"], payload["decision"]
            )
        raise ValueError("Unsupported test command")

    async def get_status(self, report_id, actor_id):
        record = await self.store.load(report_id, actor_id)
        pending = self.pending.get(report_id, ())
        if record.cancel_requested:
            status = "cancelled"
        elif pending:
            status = "paused"
        else:
            status = record.status
        return SessionStatus(report_id, status, pending)

    async def start(self, report_id, actor_id, message):
        record = await self.store.load(report_id, actor_id)
        running = replace(record, run_id="run-1", status="running")
        await self.store.save(running)
        self.entered.set()
        await self.gate.wait()
        self.pending[report_id] = (
            {
                "id": "int-1",
                "value": {
                    "action_requests": [
                        {
                            "name": "confirm_plan",
                            "args": {
                                "artifact": {
                                    "artifact_id": "plan:1",
                                    "version": "1",
                                    "content_hash": "abc",
                                }
                            },
                        }
                    ]
                },
            },
        )
        await self.store.save(replace(running, status="paused"))
        return SessionStatus(report_id, "paused", self.pending[report_id])

    async def resume(self, report_id, actor_id, decision=None):
        record = await self.store.load(report_id, actor_id)
        pending = {
            item["id"]: item["value"] for item in self.pending.get(report_id, ())
        }
        if decision is not None:
            validate_decisions(pending, decision)
        self.pending[report_id] = ()
        await self.store.save(replace(record, status="idle"))
        return SessionStatus(report_id, "idle", ())

    async def cancel(self, report_id, actor_id):
        await self.store.request_cancel(report_id, actor_id)
        record = await self.store.load(report_id, actor_id)
        await self.store.save(replace(record, status="cancelled"))
        return await self.get_status(report_id, actor_id)


class ChartStub:
    def __init__(self, store):
        self.services = SimpleNamespace(repository=store)
        self.seen: list[str] = []

    async def resolve(self, actor_id, report_id, version, chart_id):
        self.seen.append(actor_id)
        await self.services.repository.load(report_id, actor_id)
        raise LookupError("Chart unavailable")


async def _wait_status(client, report_id, predicate):
    last = None
    for _ in range(50):
        response = await client.get(f"/api/reports/{report_id}")
        assert response.status_code == 200
        last = response.json()
        if predicate(last):
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(last)


@pytest.mark.asyncio
async def test_workbench_lists_runs_decides_and_hides_other_actors(tmp_path):
    from research_copilot.runtime.storage import SqlSessionStore
    from research_copilot.web.app import WorkbenchBackend, create_app

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.sqlite'}")
    store = SqlSessionStore(engine)
    await store.setup()
    session = FakeSession(store)
    charts = ChartStub(store)
    workspace = tmp_path / "workspace"
    backend = WorkbenchBackend(
        session=session,
        store=store,
        actor_id="local",
        workspace_root=workspace,
        chart_service=charts,
        execution_policy=POLICY,
        policy_version="v2-policy-1",
    )
    app = create_app(backend=backend)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost"
        ) as client:
            health = await client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["checks"]["database"]["status"] == "ok"
            assert (await client.get("/api/reports")).json()["reports"] == []

            created = await client.post(
                "/api/reports", json={"user_request": "分析五粮液"}
            )
            assert created.status_code == 201
            report_id = created.json()["report"]["id"]
            assert created.json()["report"]["user_request"] == "分析五粮液"
            listed = (await client.get("/api/reports")).json()
            assert listed["reports"][0]["id"] == report_id
            assert listed["active_report_id"] == report_id

            await asyncio.wait_for(session.entered.wait(), timeout=2)
            assert (await client.delete(f"/api/reports/{report_id}")).status_code == 409
            assert (
                await client.post(f"/api/reports/{report_id}/resume")
            ).status_code == 409

            session.gate.set()
            paused = await _wait_status(
                client,
                report_id,
                lambda body: (
                    body["report"]["status"] == "paused" and body["interrupts"]
                ),
            )
            assert paused["interrupts"][0]["tool"] == "confirm_plan"
            assert paused["runtime"]["busy"] is False

            mismatch = await client.post(
                f"/api/reports/{report_id}/decisions",
                json={"decisions": {"other": {"decisions": [{"type": "approve"}]}}},
            )
            assert mismatch.status_code == 400

            record = await store.load(report_id, "local")
            context = RunContext(
                record.actor_id,
                record.report_id,
                record.run_id,
                record.budget_id,
                record.policy_version,
            )

            def seed(state):
                state.role_data["evidence"] = {
                    "E-000001": {
                        "evidence_id": "E-000001",
                        "content_hash": "abc",
                        "raw": {
                            "title": "公告",
                            "encoding": "utf-8",
                            "body": ("a" * 200) + SECRET,
                        },
                        "kind": "web",
                        "parent_id": None,
                        "collected_at": "2026-01-01T00:00:00+00:00",
                        "source": {"server": "tavily", "tool": "search"},
                    }
                }
                ref = ArtifactRef(
                    artifact_id="report:1", version="1", content_hash="a" * 64
                )
                state.artifacts["report"] = ref
                state.role_data["objects"] = {
                    "rep": {
                        "kind": "report",
                        "ref": ref.model_dump(),
                        "payload": {"document": "# 标题\n\n正文"},
                    }
                }

            await store.mutate_control(context, seed)
            page = await client.get(f"/api/reports/{report_id}/evidence")
            assert page.status_code == 200
            body = page.json()
            assert body["total"] == 1
            assert SECRET not in page.text
            assert body["records"][0]["title"] == "公告"
            detail = await client.get(f"/api/reports/{report_id}/evidence/E-000001")
            assert SECRET in detail.json()["text"]

            report = await client.get(f"/api/reports/{report_id}/report")
            assert report.status_code == 200
            assert "正文" in report.json()["content"]
            assert report.json()["version"] == "1"

            other = SessionRecord(
                "other-report",
                "someone-else",
                "v2",
                "v2-policy-1",
                "other-report",
                user_request="hidden",
                created_at="2020-01-01T00:00:00+00:00",
            )
            await store.create(
                other, TokenBudget(limit=100, budget_id="other-report").snapshot()
            )
            assert (await client.get("/api/reports/other-report")).status_code == 404
            assert (
                await client.get("/api/reports/other-report/report")
            ).status_code == 404
            chart = await client.get(
                f"/api/reports/{report_id}/versions/1/charts/C-0123456789abcdef"
            )
            hidden_chart = await client.get(
                "/api/reports/other-report/versions/1/charts/C-0123456789abcdef"
            )
            assert chart.status_code == 404
            assert hidden_chart.status_code == 404
            assert charts.seen == ["local", "local"]

            accepted = await client.post(
                f"/api/reports/{report_id}/decisions",
                json={"decisions": {"int-1": {"decisions": [{"type": "approve"}]}}},
            )
            assert accepted.status_code == 200
            await _wait_status(
                client, report_id, lambda item: item["report"]["status"] == "idle"
            )
            cancelled = await client.post(f"/api/reports/{report_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["state"] == "cancelled"

            (workspace / report_id).mkdir(parents=True)
            (workspace / report_id / "note.txt").write_text("x", encoding="utf-8")
            removed = await client.delete(f"/api/reports/{report_id}")
            assert removed.status_code == 200
            payload = removed.json()
            assert payload["deleted"] is True
            assert payload["cleanup"]["checkpoints"] is False
            assert payload["cleanup"]["workspace"] is True
            assert not (workspace / report_id).exists()
            assert (await client.get(f"/api/reports/{report_id}")).status_code == 404
    finally:
        await backend.runs.aclose()
        await engine.dispose()
