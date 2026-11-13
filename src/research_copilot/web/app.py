"""Local single-user workbench. Model calls run in the background; reads do not."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from research_copilot import __version__
from research_copilot.sources.health import SourcesUnavailable, require_available_sources, source_health
from research_copilot.runtime.preflight import new_budget
from research_copilot.runtime.budget import BudgetExceeded
from research_copilot.runtime.retrospective_recovery import (
    RecoveryRequest,
    RecoveryConflict,
)
from research_copilot.runtime.session import SessionRecord, validate_decisions
from research_copilot.runtime.storage import SessionBusy
from research_copilot.runtime.task_completion import await_completion
from research_copilot.web.runs import DurableRuns
from research_copilot.runtime.run_queue import QueueConflict, QueueFull
from research_copilot.runtime.run_recovery import (
    ReleaseRequest,
    RequestRecoveryDecision,
    RunRecovery,
)
from research_copilot.web.charts import create_chart_router
from research_copilot.web.workspace import (
    build_workspace,
    evidence_body,
    evidence_records,
    report_summary,
    select_report,
)
from research_copilot.workspace import safe_component
from research_copilot.workspace.filesystem import reject_links, tree_files

from research_copilot.runtime.identity import DEFAULT_POLICY_VERSION, RUNTIME_VERSION

logger = logging.getLogger(__name__)


class WorkbenchBackend:
    def __init__(
        self,
        *,
        session,
        store,
        actor_id: str,
        workspace_root: Path,
        chart_service=None,
        experience=None,
        token_limit: int | None = None,
        policy_version: str = DEFAULT_POLICY_VERSION,
        execution_policy: Path = Path("config/execution_policy.toml"),
        history_only: bool = False,
        models_ready: bool = True,
        database_ready: bool = True,
    ):
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError("A local actor id is required")
        safe_component(actor_id)
        self.session = session
        self.store = store
        self.experience = experience
        self.actor_id = actor_id
        self.workspace_root = Path(workspace_root)
        self.chart_service = chart_service
        self.token_limit = token_limit
        self.policy_version = policy_version
        self.execution_policy = Path(execution_policy)
        self.history_only = history_only
        self.models_ready = models_ready and not history_only
        self.database_ready = database_ready
        self.runs = DurableRuns(session, store, actor_id)
        self._probe_lock = asyncio.Lock()
        self._probe_until = 0.0
        self._probe_ok = False
        self.last_database_success = None
        self.source_statuses = getattr(session, "source_statuses", None)
        self.source_checked_at = getattr(session, "source_checked_at", None)

    async def database_available(self):
        watch = getattr(self.store, "runtime_watch", None)
        if watch is not None and not watch.available:
            return False
        if not self.database_ready:
            return False
        async with self._probe_lock:
            if time.monotonic() >= self._probe_until:
                try:
                    async with asyncio.timeout(1):
                        await self.store.probe()
                except Exception:
                    self._probe_ok = False
                else:
                    self._probe_ok = True
                    self.last_database_success = datetime.now(UTC).isoformat()
                self._probe_until = time.monotonic() + 1
            return self._probe_ok


class _ChartAdapter:
    def __init__(self, app: FastAPI):
        self.app = app

    @property
    def services(self):
        service = self.app.state.backend.chart_service
        if service is None:
            raise LookupError("Chart unavailable")
        return service.services

    async def resolve(self, *args, **kwargs):
        service = self.app.state.backend.chart_service
        if service is None:
            raise LookupError("Chart unavailable")
        return await service.resolve(*args, **kwargs)


def actor_dependency(request: Request) -> str:
    backend = getattr(request.app.state, "backend", None)
    actor_id = getattr(backend, "actor_id", "")
    if not isinstance(actor_id, str) or not actor_id:
        raise HTTPException(401, "Authentication required")
    return actor_id


def _backend(request: Request) -> WorkbenchBackend:
    backend = getattr(request.app.state, "backend", None)
    if backend is None:
        raise HTTPException(503, "Workbench is not ready")
    return backend


def _report_id(report_id: str) -> str:
    try:
        return safe_component(report_id)
    except ValueError:
        raise HTTPException(404, "Report unavailable") from None


def _context(record):
    from research_copilot.contracts import RunContext

    return RunContext(
        record.actor_id,
        record.report_id,
        record.run_id,
        record.budget_id,
        record.policy_version,
    )


async def _load(backend: WorkbenchBackend, report_id: str):
    try:
        return await backend.store.load(report_id, backend.actor_id)
    except LookupError:
        raise HTTPException(404, "Report unavailable") from None


async def _control(backend: WorkbenchBackend, record):
    try:
        return await backend.store.read_control(_context(record))
    except LookupError:
        raise HTTPException(404, "Report unavailable") from None


def _experience(request: Request):
    current = _backend(request)
    service = getattr(current, "experience", None)
    if service is None:
        raise HTTPException(503, "经验存储未初始化")
    return current, service


def _check(ready: bool, detail: str) -> dict:
    return {"status": "ok" if ready else "not_configured", "detail": detail}


def create_app(
    *,
    backend: WorkbenchBackend | None = None,
    lifespan=None,
    static_dir: Path | None = None,
) -> FastAPI:
    from research_copilot.runtime import web_release

    app = FastAPI(title="Research Copilot", version=__version__, lifespan=lifespan)
    app.state.web_release = {
        "application_version": __version__,
        "api_version": web_release.API_VERSION,
        "release_id": None,
        "static_verified": False,
    }

    @app.get("/api/release")
    async def release_info():
        return app.state.web_release

    if backend is not None:
        app.state.backend = backend

    @app.exception_handler(QueueFull)
    async def queue_full(request, exc):
        return JSONResponse(
            status_code=429,
            content={"detail": "研究等待队列已满，请稍后重试"},
            headers={"Retry-After": "2"},
        )

    @app.exception_handler(QueueConflict)
    async def queue_conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    app.include_router(create_chart_router(_ChartAdapter(app), actor_dependency))

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        current_backend = getattr(request.app.state, "backend", None)
        if getattr(current_backend, "history_only", False) and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            return JSONResponse({"detail": "只读浏览服务不接受写入。"}, status_code=409)
        # The fixed local actor is not remote authentication. Protect reads too:
        # DNS rebinding must not expose reports through an arbitrary Host.
        host = request.headers.get("host", "").lower()
        origin = request.headers.get("origin")
        local = re.fullmatch(
            r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host
        )
        if (
            not local
            or len(request.headers.getlist("host")) != 1
            or len(request.headers.getlist("origin")) > 1
            or len(request.headers.getlist("sec-fetch-site")) > 1
            or (origin is not None and origin != f"{request.url.scheme}://{host}")
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            return JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
        current = getattr(request.app.state, "backend", None)
        watch = getattr(getattr(current, "store", None), "runtime_watch", None)
        if (
            watch is not None
            and not watch.available
            and request.url.path.startswith("/api/")
            and request.url.path not in {"/api/live", "/api/health", "/api/ready"}
        ):
            return JSONResponse(
                {"detail": "宿主正在停止或已失去所有权，请核查服务状态"},
                status_code=503,
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            match = re.match(r"^/api/reports/([^/]+)(?:/|$)", request.url.path)
            if match and current is not None:
                try:
                    view = await current.store.read_snapshot(
                        match.group(1), current.actor_id
                    )
                except LookupError:
                    return JSONResponse(
                        {"detail": "Report unavailable"}, status_code=404
                    )
                if view.state.role_data.get("workflow_version") != "products-v2":
                    return JSONResponse(
                        {"detail": "历史会话只读，请新建研究会话。"}, status_code=409
                    )
            if (
                request.method in {"POST", "PUT", "PATCH"}
                and (
                    request.headers.get("content-length", "0") != "0"
                    or "transfer-encoding" in request.headers
                )
                and request.headers.get("content-type", "")
                .split(";")[0]
                .strip()
                .lower()
                != "application/json"
            ):
                return JSONResponse({"detail": "请求正文必须是 JSON"}, status_code=415)
        return await call_next(request)

    @app.get("/api/live")
    async def live():
        return {"status": "ok"}

    @app.get("/api/health")
    async def health(request: Request):
        current = getattr(request.app.state, "backend", None)
        database_ready = bool(current and await current.database_available())
        models_ready = bool(current and current.models_ready)
        sources = source_health(
            current.source_statuses if current else (),
            current.source_checked_at if current else None,
        )
        ok = database_ready and models_ready and current is not None and sources["ready"]
        return {
            "status": "ok" if ok else "degraded",
            "service": "research-copilot",
            "version": __version__,
            "environment": "local",
            "last_database_success": current.last_database_success if current else None,
            "checked_at": datetime.now(UTC).isoformat(),
            "message": "本地单用户工作台" if ok else "数据库、模型配置或数据源未就绪",
            "sources": sources,
            "checks": {
                "api": _check(current is not None, ""),
                "database": _check(
                    database_ready, "" if database_ready else "数据库未连接"
                ),
                "model": _check(models_ready, "" if models_ready else "模型配置未就绪"),
                "sources": {"status": "ok" if sources["ready"] else "unavailable", "detail": sources["detail"]},
                "workflow": _check(True, RUNTIME_VERSION),
            },
        }

    @app.get("/api/ready")
    async def ready(request: Request):
        payload = await health(request)
        return JSONResponse(
            payload, status_code=200 if payload["status"] == "ok" else 503
        )

    @app.get("/api/reports")
    async def list_reports(request: Request):
        current = _backend(request)
        records = await current.store.list_for_actor(current.actor_id)
        versions = await current.store.workflow_versions(current.actor_id)
        active_reports = await current.runs.queue.active_reports(current.actor_id)
        active = next(
            (
                record.report_id
                for record in records
                if record.report_id in active_reports
            ),
            None,
        )
        return {
            "reports": [
                {
                    **report_summary(record, active=record.report_id == active),
                    "read_only": current.history_only
                    or versions.get(record.report_id) != "products-v2",
                }
                for record in records
            ],
            "active_report_id": active,
        }

    @app.post("/api/reports", status_code=201)
    async def create_report(request: Request):
        current = _backend(request)
        body = await request.json()
        message = body.get("user_request") if isinstance(body, dict) else None
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise HTTPException(
                400, "A non-empty message of at most 8000 characters is required"
            )
        try:
            require_available_sources(current.source_statuses)
        except SourcesUnavailable as exc:
            return JSONResponse({"code": exc.code, "detail": str(exc)}, status_code=503)
        key = request.headers.get("idempotency-key")
        if key is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", key):
            raise HTTPException(400, "创建请求标识无效")
        report_id = (
            hashlib.sha256(f"{current.actor_id}\0{key}".encode()).hexdigest()[:32]
            if key is not None
            else uuid4().hex
        )
        record = SessionRecord(
            report_id,
            current.actor_id,
            RUNTIME_VERSION,
            current.policy_version,
            report_id,
            user_request=message.strip(),
            created_at=datetime.now(UTC).isoformat(),
        )

        record = await current.runs.create(
            record,
            new_budget(
                report_id,
                token_limit=current.token_limit,
                policy_path=current.execution_policy,
            ).snapshot(),
        )
        return {
            "report": report_summary(record, active=await current.runs.busy(report_id))
        }

    @app.get("/api/reports/{report_id}")
    async def workspace(report_id: str, request: Request):
        report_id = _report_id(report_id)
        try:
            return await build_workspace(_backend(request), report_id)
        except LookupError:
            raise HTTPException(404, "Report unavailable") from None

    @app.post("/api/reports/{report_id}/decisions")
    async def decisions(report_id: str, request: Request):
        report_id = _report_id(report_id)
        current = _backend(request)
        await _load(current, report_id)
        body = await request.json()
        supplied = body.get("decisions") if isinstance(body, dict) else None
        if not isinstance(supplied, dict):
            raise HTTPException(400, "Decisions must be an object")
        snapshot = await current.session.get_status(report_id, current.actor_id)
        pending = {item["id"]: item["value"] for item in snapshot.interrupts}
        try:
            validate_decisions(pending, supplied)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if await current.runs.busy(report_id):
            raise HTTPException(409, "Report is already running")

        try:
            await current.runs.submit(report_id, "resume", {"decision": supplied})
        except SessionBusy:
            raise HTTPException(409, "Report is already running") from None
        return {"action": "decisions", "state": "running"}

    @app.get("/api/reports/{report_id}/events")
    async def events(report_id: str, request: Request):
        report_id = _report_id(report_id)
        current = _backend(request)
        await _load(current, report_id)
        from research_copilot.web.stream import workspace_events

        return StreamingResponse(
            workspace_events(
                current,
                report_id,
                request.is_disconnected,
                last_event_id=request.headers.get("last-event-id", ""),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/reports/{report_id}/messages")
    async def message(report_id: str, request: Request):
        report_id = _report_id(report_id)
        current = _backend(request)
        record = await _load(current, report_id)
        body = await request.json()
        content = body.get("content") if isinstance(body, dict) else None
        ident = body.get("id") if isinstance(body, dict) else None
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > 8000
            or not isinstance(ident, str)
        ):
            raise HTTPException(400, "消息不能为空，且不能超过 8000 字。")
        try:
            safe_component(ident)
        except ValueError:
            raise HTTPException(400, "消息标识无效。") from None
        if record.cancel_requested:
            raise HTTPException(409, "该对话已取消，请新建对话。")
        state = await _control(current, record)
        previous = next(
            (m for m in state.role_data.get("conversation", []) if m["id"] == ident),
            None,
        )
        if previous:
            if previous["role"] != "user" or previous["content"] != content.strip():
                raise HTTPException(409, "同一消息标识不能用于不同内容。")
            return {"action": "message", "state": record.status}
        if await current.runs.busy(report_id):
            raise HTTPException(409, "正在回复，请稍候或停止当前研究。")
        if not record.run_id:
            raise HTTPException(409, "会话尚未启动，请新建研究对话。")
        if state.artifacts.get("plan"):
            raise HTTPException(409, "请先处理当前计划，调整范围请新建对话。")
        status = await current.session.get_status(report_id, current.actor_id)
        if status.interrupts:
            raise HTTPException(409, "请先处理对话中的待确认事项。")

        try:
            await current.runs.submit(
                report_id,
                "message",
                {"message": content.strip(), "message_id": ident},
                operation_id="message-"
                + hashlib.sha256(f"{report_id}\0{ident}".encode()).hexdigest(),
            )
        except SessionBusy:
            raise HTTPException(409, "正在回复，请稍候。") from None
        return {"action": "message", "state": "running"}

    @app.post("/api/reports/{report_id}/correct")
    async def correct(report_id: str, request: Request):
        current = _backend(request)
        report_id = _report_id(report_id)
        record = await _load(current, report_id)
        body = await request.json()
        ident = body.get("dispatch_call_id") if isinstance(body, dict) else None
        if not isinstance(ident, str) or not re.fullmatch(r"wf-[a-f0-9]{32}", ident):
            raise HTTPException(400, "失败派发标识无效")
        state = await _control(current, record)
        receipt = state.role_data.get("dispatch_recoveries", {}).get(ident)
        if (
            receipt
            and receipt.get("actor_id") == record.actor_id
            and receipt.get("run_id") == record.run_id
        ):
            return {
                "action": "correct",
                "state": record.status,
                "authorization_id": receipt["authorization_id"],
            }

        try:
            authorization_id = await current.runs.correct(report_id, ident)
        except (SessionBusy, ValueError) as exc:
            raise HTTPException(409, str(exc)) from None
        return {
            "action": "correct",
            "state": "running",
            "authorization_id": authorization_id,
        }

    @app.get("/api/reports/{report_id}/run-recovery")
    async def run_recovery(report_id: str, request: Request):
        current = _backend(request)
        try:
            return await RunRecovery(current.store).inspect(
                _report_id(report_id), current.actor_id
            )
        except LookupError as exc:
            raise HTTPException(404, "报告不存在") from exc

    @app.post("/api/reports/{report_id}/runs/{job_id}/release")
    async def release_run(
        report_id: str, job_id: str, body: ReleaseRequest, request: Request
    ):
        current = _backend(request)
        try:
            return await current.runs.release(
                _report_id(report_id), _report_id(job_id), body
            )
        except LookupError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except (SessionBusy, QueueConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/reports/{report_id}/requests/{request_id}/recovery")
    async def recover_run_request(
        report_id: str, request_id: str, body: RequestRecoveryDecision, request: Request
    ):
        current = _backend(request)
        try:
            return await current.runs.recover_request(
                _report_id(report_id), _report_id(request_id), body
            )
        except BudgetExceeded as exc:
            raise HTTPException(
                409, "核实用量已保存，但已超出授权预算；请刷新记录，研究不会自动继续。"
            ) from exc
        except LookupError as exc:
            raise HTTPException(404, "请求不存在") from exc
        except (SessionBusy, QueueConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/reports/{report_id}/resume")
    async def resume(report_id: str, request: Request):
        report_id = _report_id(report_id)
        current = _backend(request)
        record = await _load(current, report_id)
        if record.cancel_requested:
            raise HTTPException(409, "Cancelled sessions cannot resume")

        try:
            if record.status == "created" and not record.run_id:
                await current.runs.submit(
                    report_id, "start", {"message": record.user_request}
                )
            else:
                await current.runs.submit(report_id, "resume", {"decision": None})
        except SessionBusy:
            raise HTTPException(409, "Report is already running") from None
        return {"action": "resume", "state": "running"}

    @app.post("/api/reports/{report_id}/cancel")
    async def cancel(report_id: str, request: Request):
        report_id = _report_id(report_id)
        current = _backend(request)
        await _load(current, report_id)
        try:
            status = await current.session.cancel(report_id, current.actor_id)
            await current.runs.cancel_waiting(report_id)
        except LookupError:
            raise HTTPException(404, "Report unavailable") from None
        return {
            "action": "cancel",
            "state": status.status,
            "report_id": status.report_id,
        }

    @app.delete("/api/reports/{report_id}")
    async def remove(report_id: str, request: Request):
        report_id = _report_id(report_id)
        current = _backend(request)
        if await current.runs.busy(report_id):
            raise HTTPException(409, "Report is already running")
        try:
            async with current.store.lock(report_id):
                await current.store.delete_if_idle(report_id, current.actor_id)
                removed, errors = await await_completion(
                    asyncio.to_thread(
                        _remove_workspace, current.workspace_root, report_id
                    )
                )
        except LookupError:
            raise HTTPException(404, "Report unavailable") from None
        except SessionBusy:
            raise HTTPException(409, "Report is already running") from None
        return {
            "deleted": True,
            "report_id": report_id,
            "cleanup": {"checkpoints": False, "workspace": removed, "errors": errors},
        }

    @app.get("/api/reports/{report_id}/evidence")
    async def evidence(
        report_id: str, request: Request, offset: int = 0, limit: int = 20
    ):
        report_id = _report_id(report_id)
        current = _backend(request)
        record = await _load(current, report_id)
        state = await _control(current, record)
        if offset < 0 or limit < 1 or limit > 100:
            raise HTTPException(400, "Invalid evidence page")
        records = evidence_records(state)
        page = records[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(records) else None
        return {
            "total": len(records),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "records": page,
        }

    @app.get("/api/reports/{report_id}/evidence/{evidence_id}")
    async def evidence_detail(
        report_id: str,
        evidence_id: str,
        request: Request,
        offset: int = 0,
        limit: int = 4000,
    ):
        report_id = _report_id(report_id)
        current = _backend(request)
        record = await _load(current, report_id)
        state = await _control(current, record)
        found = evidence_body(state, evidence_id)
        if found is None:
            raise HTTPException(404, "Evidence unavailable")
        summary, body = found
        if offset < 0 or limit < 1 or limit > 20000:
            raise HTTPException(400, "Invalid evidence window")
        chunk = body[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(body) else None
        return {
            "evidence": summary,
            "text": chunk,
            "offset": offset,
            "limit": limit,
            "total_length": len(body),
            "truncated": next_offset is not None,
            "next_offset": next_offset,
        }

    @app.get("/api/reports/{report_id}/experiences")
    async def report_experiences(report_id: str, request: Request):
        report_id = _report_id(report_id)
        current, service = _experience(request)
        try:
            return await service.report_view(current.actor_id, report_id)
        except (LookupError, PermissionError):
            raise HTTPException(404, "Report unavailable") from None

    @app.post("/api/reports/{report_id}/retrospectives/{job_id}/recovery")
    async def recover_retrospective(
        report_id: str, job_id: str, body: RecoveryRequest, request: Request
    ):
        report_id, job_id = _report_id(report_id), _report_id(job_id)
        current, service = _experience(request)
        try:
            job = await service.recover(current.actor_id, report_id, job_id, body)
        except (LookupError, PermissionError):
            raise HTTPException(404, "复盘任务不存在") from None
        except RecoveryConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"job": job}

    @app.patch("/api/experiences/{experience_id}")
    async def update_experience(experience_id: str, request: Request):
        from research_copilot.workspace import safe_component

        try:
            experience_id = safe_component(experience_id)
        except ValueError:
            raise HTTPException(404, "Experience unavailable") from None
        current, service = _experience(request)
        body = await request.json()
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled 必须是布尔值")
        try:
            record = await service.set_enabled(current.actor_id, experience_id, enabled)
        except (LookupError, PermissionError):
            raise HTTPException(404, "Experience unavailable") from None
        return {"experience": record}

    @app.get("/api/reports/{report_id}/report")
    async def report_text(report_id: str, request: Request, version: str | None = None):
        report_id = _report_id(report_id)
        current = _backend(request)
        record = await _load(current, report_id)
        state = await _control(current, record)
        chosen = select_report(state, version)
        if chosen is None:
            raise HTTPException(404, "Report text unavailable")
        content = chosen["document"]
        return {
            "version": chosen["version"],
            "is_frozen": chosen["frozen"],
            "file_path": None,
            "content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "manifest_hash": chosen["content_hash"],
        }

    _mount_frontend(app, static_dir)
    return app


def _remove_workspace(root: Path, report_id: str) -> tuple[bool, list[str]]:
    try:
        # Reject aliases before resolving: an in-workspace link can name another
        # report and would otherwise turn authorized cleanup into sibling deletion.
        base = reject_links(root).resolve()
        target = reject_links(base / safe_component(report_id)).resolve()
        if target == base or not target.is_relative_to(base):
            return False, ["workspace path escaped"]
        if not target.exists():
            return True, []
        for _ in tree_files(target):
            pass
        shutil.rmtree(target)
    except (OSError, ValueError) as exc:
        return False, [type(exc).__name__]
    return True, []


def _mount_frontend(app: FastAPI, static_dir: Path | None) -> None:
    if static_dir is None:
        return
    from research_copilot.runtime.web_release import load_web_release, MANIFEST

    files, summary = load_web_release(app, static_dir)
    app.state.web_release = summary

    def asset(name):
        raw = files[name]
        return Response(
            raw,
            media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            headers={
                "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/", include_in_schema=False)
    async def index_page():
        return asset("index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if (
            path == "api"
            or path.startswith("api/")
            or path == MANIFEST
            or "\\" in path
            or ".." in Path(path).parts
        ):
            raise HTTPException(404, "Not found")
        if path in files:
            return asset(path)
        if path.startswith("assets/") or Path(path).suffix:
            raise HTTPException(404, "Not found")
        return asset("index.html")


def build_server_app(
    *,
    settings,
    models_path: Path | None,
    workspace_root: Path,
    actor_id: str,
    policy_version: str,
    token_limit: int | None,
    mode: str,
    rules_path: Path,
    rules_approval_path: Path | None,
    execution_policy_path: Path,
    static_dir: Path | None,
    history_only: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from research_copilot.host import open_session
        from research_copilot.web.charts import ChartService

        if history_only:
            from research_copilot.web.history import open_history

            async with open_history(
                settings, workspace_root=workspace_root, policy_version=policy_version
            ) as (session, store, services):
                app.state.backend = WorkbenchBackend(
                    session=session,
                    store=store,
                    actor_id=actor_id,
                    workspace_root=workspace_root,
                    chart_service=ChartService(services),
                    experience=services.experience,
                    policy_version=policy_version,
                    history_only=True,
                )
                yield
            return
        async with open_session(
            settings,
            models_path=models_path,
            workspace_root=workspace_root,
            policy_version=policy_version,
            initialize=False,
            mode=mode,
            rules_path=rules_path,
            rules_approval_path=rules_approval_path,
            execution_policy_path=execution_policy_path,
        ) as (session, store, services):
            app.state.backend = WorkbenchBackend(
                session=session,
                store=store,
                actor_id=actor_id,
                workspace_root=workspace_root,
                chart_service=ChartService(services),
                experience=services.experience,
                token_limit=token_limit,
                policy_version=policy_version,
                execution_policy=execution_policy_path,
            )
            worker = None
            try:
                await app.state.backend.runs.start()
                extractor = getattr(services, "experience_extractor", None)
                if services.experience is not None and extractor is not None:
                    from research_copilot.runtime.experience import RetrospectiveWorker

                    worker = RetrospectiveWorker(store, extractor)
                    await worker.start()
                yield
            finally:
                try:
                    await app.state.backend.runs.aclose()
                except TimeoutError:
                    # Do not close clients/pools underneath a delayed settlement.
                    # A process manager may force exit; pending recovery handles it.
                    logger.critical(
                        "Report shutdown deadline exceeded; dependencies remain open until tasks drain"
                    )
                    await app.state.backend.runs.drain()
                if worker is not None:
                    await worker.stop()

    return create_app(lifespan=lifespan, static_dir=static_dir)
