"""Read frozen chart assets by report version and chart ID, never arbitrary paths."""

import hashlib
import json
from pathlib import PurePosixPath
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from research_copilot.contracts import RunContext
from research_copilot.subagents.services import digest
from research_copilot.workspace.filesystem import reject_links

from research_copilot.runtime.identity import RUNTIME_VERSION


class ChartService:
    def __init__(self, services):
        self.services = services

    async def resolve(self, actor_id, report_id, version, chart_id):
        record = await self.services.repository.load(report_id, actor_id)
        if record.runtime_version != RUNTIME_VERSION or record.actor_id != actor_id:
            raise LookupError("Chart unavailable")
        context = RunContext(
            record.actor_id,
            record.report_id,
            record.run_id,
            record.budget_id,
            record.policy_version,
        )
        state = await self.services.repository.read_control(context)
        objects = state.role_data.get("objects", {})
        reports = [
            obj
            for obj in objects.values()
            if obj["kind"] == "report" and str(obj["ref"]["version"]) == version
        ]
        if len(reports) != 1:
            raise LookupError("Report version unavailable")
        report = reports[0]
        self._verify_object(context, report)
        if chart_id not in report["payload"].get("chart_ids", []):
            raise LookupError("Chart is outside selected report version")
        analysis = objects[report["payload"]["analysis_ids"][0]]
        self._verify_object(context, analysis)
        if (
            report["payload"]["analysis_ref"] != analysis["ref"]
            or chart_id not in analysis["payload"]["chart_ids"]
        ):
            raise ValueError("Frozen analysis reference mismatch")
        chart = state.role_data["charts"][analysis["dispatch_id"]][chart_id]
        receipt = chart["receipt"]
        if execution_id := receipt.get("execution_id"):
            from research_copilot.sandbox.receipts import verify_execution

            execution = state.role_data.get("executions", {}).get(execution_id)
            if (
                not execution
                or execution["dispatch_id"] != analysis["dispatch_id"]
                or chart_id not in execution["response"]["chart_ids"]
            ):
                raise ValueError("Invalid chart execution")
            verify_execution(self.services.report_root(context), execution)
        assets = {}
        for relative, expected in chart["files"].items():
            raw = reject_links(
                self.services.report_root(context) / relative
            ).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Chart artifact hash mismatch")
            assets[relative] = raw
        if "manifest_path" in chart:
            base = PurePosixPath(chart["manifest_path"]).parent
            paths = {"png": (base / receipt["static_file"]).as_posix()}
            if receipt.get("option_file"):
                paths["option"] = (base / receipt["option_file"]).as_posix()
            if receipt.get("data_file"):
                paths["data"] = (base / receipt["data_file"]).as_posix()
        else:
            # Historical fixed-tool chart receipts remain readable without recomputing.
            png = [p for p in assets if p.endswith(".png")]
            if len(png) != 1:
                raise LookupError("Static chart unavailable")
            paths = {"png": png[0]}
        return {
            "receipt": receipt,
            "content_hash": digest(chart["files"]),
            "paths": paths,
            "assets": assets,
        }

    def _verify_object(self, context, obj):
        path = obj["path"]
        raw = reject_links(
            self.services.report_root(context) / path.removeprefix("/workspace/")
        ).read_bytes()
        if (
            hashlib.sha256(raw).hexdigest() != obj["ref"]["content_hash"]
            or json.loads(raw) != obj["payload"]
        ):
            raise ValueError("Report artifact hash mismatch")


class _StoreView:
    """Resolve the repository on each call so the router can be built before the host opens."""

    def __init__(self, resolve):
        self._resolve = resolve

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


def create_chart_router(service, actor_dependency):
    """Authentication is mandatory; there is deliberately no header/default actor fallback."""
    if not callable(actor_dependency):
        raise ValueError("Authenticated actor dependency required")
    router = APIRouter()
    from research_copilot.web.status import create_status_router

    router.include_router(
        create_status_router(
            _StoreView(lambda: service.services.repository), actor_dependency
        )
    )

    async def resolve(actor_id, report_id, version, chart_id):
        if not isinstance(actor_id, str) or not actor_id:
            raise HTTPException(401, "Authentication required")
        try:
            return await service.resolve(actor_id, report_id, version, chart_id)
        except (LookupError, KeyError, PermissionError):
            raise HTTPException(404, "Chart unavailable") from None
        except (ValueError, OSError):
            raise HTTPException(409, "Frozen chart integrity check failed") from None

    @router.get("/api/reports/{report_id}/versions/{version}/charts/{chart_id}")
    async def manifest(
        report_id: str,
        version: str,
        chart_id: str,
        actor_id: str = Depends(actor_dependency),
    ):
        item = await resolve(actor_id, report_id, version, chart_id)
        receipt = item["receipt"]
        prefix = f"/api/reports/{report_id}/versions/{version}/charts/{chart_id}"
        return {
            "report_id": report_id,
            "report_version": version,
            "chart_id": chart_id,
            "engine": receipt.get("engine", "matplotlib"),
            "title": receipt.get("title", chart_id),
            "alt": receipt.get("alt", receipt.get("title", chart_id)),
            "content_hash": item["content_hash"],
            "static_url": prefix + "/png",
            "option_url": prefix + "/option" if "option" in item["paths"] else None,
            "theme_version": receipt.get("theme_version", "research-v1"),
        }

    @router.get("/api/reports/{report_id}/versions/{version}/charts/{chart_id}/{asset}")
    async def asset(
        report_id: str,
        version: str,
        chart_id: str,
        asset: str,
        actor_id: str = Depends(actor_dependency),
    ):
        item = await resolve(actor_id, report_id, version, chart_id)
        if asset not in item["paths"]:
            raise HTTPException(404, "Chart asset unavailable")
        raw = item["assets"][item["paths"][asset]]
        return Response(
            raw,
            media_type="image/png" if asset == "png" else "application/json",
            headers={
                "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=3600",
            },
        )

    return router
