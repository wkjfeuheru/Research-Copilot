"""Version-bound chart reads use authenticated report ownership and frozen references."""

import importlib.util
import hashlib
import httpx
import pytest
from fastapi import FastAPI
from tests.unit.test_orchestration import context


@pytest.mark.asyncio
async def test_chart_route_rejects_other_actor_report_and_version(role_env):
    assert importlib.util.find_spec("research_copilot.web"), (
        "authenticated chart API missing"
    )
    from research_copilot.web.charts import ChartService, create_chart_router

    services, _, store = role_env
    raw = b"frozen-png-for-route-test"
    path = services.report_root(context()) / "artifacts/chart.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    relative = "artifacts/chart.png"
    expected = hashlib.sha256(raw).hexdigest()
    cid = "C-" + "1" * 16
    from research_copilot.subagents.services import canonical, digest

    analysis_payload = {"chart_ids": [cid]}
    analysis_ref = {
        "artifact_id": "analysis-1",
        "version": "1",
        "content_hash": digest(analysis_payload),
    }
    report_payload = {
        "chart_ids": [cid],
        "analysis_ids": ["analysis-1"],
        "analysis_ref": analysis_ref,
    }
    (path.parent / "analysis.json").write_text(
        canonical(analysis_payload), encoding="utf8"
    )
    (path.parent / "report.json").write_text(canonical(report_payload), encoding="utf8")

    def setup(state):
        state.role_data["objects"] = {
            "draft-1": {
                "kind": "report",
                "path": "/workspace/artifacts/report.json",
                "ref": {
                    "artifact_id": "draft-1",
                    "version": "1",
                    "content_hash": digest(report_payload),
                },
                "payload": report_payload,
            },
            "analysis-1": {
                "kind": "analysis",
                "dispatch_id": "analysis-dispatch",
                "path": "/workspace/artifacts/analysis.json",
                "ref": analysis_ref,
                "payload": analysis_payload,
            },
        }
        state.role_data["charts"] = {
            "analysis-dispatch": {
                cid: {
                    "receipt": {
                        "chart_id": cid,
                        "engine": "matplotlib",
                        "title": "Revenue",
                        "alt": "Revenue",
                        "static_file": "chart.png",
                    },
                    "files": {relative: expected},
                }
            }
        }

    await store.mutate_control(context(), setup)

    async def actor():
        return "actor-1"

    app = FastAPI()
    app.include_router(create_chart_router(ChartService(services), actor))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        prefix = f"/api/reports/report-1/versions/1/charts/{cid}"
        response = await client.get(prefix)
        assert response.status_code == 200, response.text
        assert response.json()["static_url"] == prefix + "/png"
        assert (await client.get(prefix + "/png")).content == raw
        assert (
            await client.get(prefix.replace("/versions/1/", "/versions/2/"))
        ).status_code == 404
        assert (
            await client.get(prefix.replace("report-1", "other-report"))
        ).status_code == 404
        assert (
            await client.get(prefix.replace(cid, "C-" + "2" * 16))
        ).status_code == 404
        app.dependency_overrides[actor] = lambda: "other-actor"
        assert (await client.get(prefix)).status_code == 404
        app.dependency_overrides.clear()
        path.write_bytes(b"tampered")
        assert (await client.get(prefix + "/png")).status_code == 409
