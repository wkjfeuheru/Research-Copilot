"""Read-only workbench projection. Does not call a model or resume a graph."""

import json
import logging
from decimal import Decimal

from research_copilot.contracts import RunContext
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.web.status import status_payload

logger = logging.getLogger(__name__)


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            lines.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("text") or item.get("statement") or item.get("name") or ""
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
    return lines


def _artifact(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    artifact_id = value.get("artifact_id")
    version = value.get("version")
    content_hash = value.get("content_hash")
    if not all(
        isinstance(item, str) and item for item in (artifact_id, version, content_hash)
    ):
        return None
    return {
        "artifact_id": artifact_id,
        "version": version,
        "content_hash": content_hash,
    }


def normalize_interrupts(pending) -> list[dict]:
    views: list[dict] = []
    for item in pending or ():
        if not isinstance(item, dict):
            continue
        ident = item.get("id")
        value = item.get("value")
        if not isinstance(ident, str) or not isinstance(value, dict):
            continue
        if value.get("kind") == "clarification":
            views.append(
                {
                    "id": ident,
                    "kind": "clarification",
                    "question": value.get("question", ""),
                    "tool": None,
                    "artifact": None,
                    "actions": [],
                    "review": None,
                }
            )
            continue
        if value.get("kind") == "reviewer_deep_review":
            assessment = _mapping(value.get("assessment"))
            issues = assessment.get("issues")
            issues = issues if isinstance(issues, list) else []
            views.append(
                {
                    "id": ident,
                    "kind": "reviewer_deep_review",
                    "tool": None,
                    "artifact": None,
                    "actions": [],
                    "review": {
                        "review_session_id": value.get("review_session_id"),
                        "snapshot_id": (value.get("snapshot") or {}).get("snapshot_id"),
                        "checks_complete": assessment.get("checks_complete"),
                        "allowed_decisions": list(
                            value.get("allowed_decisions")
                            or ["approve", "request_changes", "reject"]
                        ),
                        "summary": assessment.get("summary") or "",
                        "issues": [
                            _issue(issue) for issue in issues if isinstance(issue, dict)
                        ],
                        "errors": list(value.get("errors") or []),
                    },
                }
            )
            continue
        actions = []
        for action in value.get("action_requests") or []:
            if not isinstance(action, dict):
                continue
            args = _mapping(action.get("args"))
            actions.append(
                {
                    "name": action.get("name"),
                    "artifact": _artifact(args.get("artifact")),
                }
            )
        views.append(
            {
                "id": ident,
                "kind": "tool",
                "tool": actions[0]["name"] if actions else None,
                "artifact": actions[0]["artifact"] if actions else None,
                "actions": actions,
                "review": None,
            }
        )
    return views


def _issue(issue: dict) -> dict:
    return {
        "issue_id": issue.get("issue_id") or "",
        "category": issue.get("category") or "",
        "severity": issue.get("severity") or "",
        "location": issue.get("location") or "",
        "quote": issue.get("quote") or "",
        "reason": issue.get("reason") or issue.get("basis") or "",
        "suggestion": issue.get("suggestion") or "",
        "hard": bool(issue.get("hard")),
    }


def _budget(value):
    if not isinstance(value, dict):
        return None
    budget = dict(value)
    cost = budget.get("estimated_cost")
    if isinstance(cost, Decimal):
        budget["estimated_cost"] = format(cost, "f")
    return budget


def project_plan(state) -> dict | None:
    ref = state.artifacts.get("plan")
    if ref is None:
        return None
    entries = [item for item in state.plans if item.get("artifact") == ref.model_dump()]
    if len(entries) != 1:
        return None
    plan = entries[0].get("plan") or {}
    tasks = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tasks.append(
            {
                "task_id": task.get("task_id") or "",
                "role": task.get("role") or "",
                "objective": task.get("objective") or "",
            }
        )
    return {
        "report_type": plan.get("report_type"),
        "objective": plan.get("objective") or "",
        "version": ref.version,
        "content_hash": ref.content_hash,
        "approved": state.approved.get("plan") == ref,
        "tasks": tasks,
    }


def project_analysis(state) -> dict | None:
    ref = state.artifacts.get("analysis")
    objects = state.role_data.get("objects", {})
    chosen = None
    if ref is not None:
        dumped = ref.model_dump()
        for obj in objects.values():
            if obj.get("kind") == "analysis" and obj.get("ref") == dumped:
                chosen = obj
                break
    if chosen is None:
        analyses = [obj for obj in objects.values() if obj.get("kind") == "analysis"]
        chosen = analyses[-1] if analyses else None
    if chosen is not None:
        payload = chosen.get("payload") or {}
        artifact = _artifact(chosen.get("ref")) or {}
        approved = False
        if ref is not None and artifact:
            approved = state.approved.get("analysis") == ref
        return {
            "version": artifact.get("version"),
            "content_hash": artifact.get("content_hash"),
            "approved": approved,
            "summary": payload.get("summary") or "",
            "conclusions": _strings(payload.get("conclusions")),
            "assumptions": _strings(payload.get("assumptions")),
            "limitations": _strings(payload.get("limitations")),
            "gaps": _strings(payload.get("gaps")),
        }
    result = None
    for ident, item in state.role_data.get("task_results", {}).items():
        if state.dispatches.get(ident, {}).get("role") == "analysist":
            result = item
    if not isinstance(result, dict):
        return None
    return {
        "version": None,
        "content_hash": None,
        "approved": False,
        "summary": result.get("summary") or "",
        "conclusions": _strings(result.get("conclusions")),
        "assumptions": _strings(result.get("assumptions")),
        "limitations": _strings(result.get("limitations")),
        "gaps": _strings(result.get("gaps")),
    }


def project_rework(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    tasks = []
    for task in raw.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tasks.append(
            {
                "role": task.get("role") or "",
                "objective": task.get("objective") or "",
                "issue_ids": list(task.get("issue_ids") or []),
            }
        )
    artifact = _mapping(raw.get("artifact"))
    return {
        "phase": raw.get("phase"),
        "version": artifact.get("version"),
        "tasks": tasks,
    }


_PUBLICATION_FIELDS = (
    "published_at",
    "publish_date",
    "publication_date",
    "announcement_date",
    "disclosure_date",
    "date",
)


def _publication(raw, structured) -> str | None:
    for field in _PUBLICATION_FIELDS:
        value = _text(raw.get(field))
        if value:
            return value
    for field in _PUBLICATION_FIELDS:
        value = _text(structured.get(field))
        if value:
            return value
    items = structured.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in _PUBLICATION_FIELDS:
                value = _text(item.get(field))
                if value:
                    return value
    return None


def _evidence_text(raw) -> str:
    body = _text(raw.get("body"))
    if body:
        return body
    structured = raw.get("structured")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, indent=2, default=str)
    blocks = raw.get("text_blocks")
    if isinstance(blocks, list):
        text = "\n\n".join(_text(block) for block in blocks if _text(block))
        if text:
            return text
    return json.dumps(raw, ensure_ascii=False, indent=2, default=str)


def evidence_records(state) -> list[dict]:
    records = []
    for ident, item in state.role_data.get("evidence", {}).items():
        if not isinstance(item, dict):
            continue
        raw = _mapping(item.get("raw"))
        structured = _mapping(raw.get("structured"))
        source_ref = _mapping(raw.get("source_ref"))
        title = _text(raw.get("title"))
        source = _mapping(item.get("source"))
        if not title.strip():
            title = source.get("tool") or ident
        summary = " ".join(_evidence_text(raw).split())
        if len(summary) > 180:
            summary = summary[:180] + "…"
        records.append(
            {
                "evidence_id": ident,
                "task_id": item.get("parent_id") or "",
                "source": source.get("server") or "",
                "title": title,
                "summary": summary,
                "content_kind": item.get("kind") or "",
                "content_hash": item.get("content_hash") or "",
                "collected_at": item.get("collected_at"),
                "published_at": _publication(raw, structured),
                "collection": source_ref.get("collection") or "",
                "page": structured.get("page") or source_ref.get("page"),
                "heading_path": _text(structured.get("heading_path")),
                "broker": _text(structured.get("broker")),
                "report_type": _text(structured.get("report_type")),
            }
        )
    records.sort(key=lambda record: record["evidence_id"])
    return records


def evidence_body(state, evidence_id: str) -> tuple[dict, str] | None:
    item = state.role_data.get("evidence", {}).get(evidence_id)
    if not isinstance(item, dict):
        return None
    matches = [
        record
        for record in evidence_records(state)
        if record["evidence_id"] == evidence_id
    ]
    if len(matches) != 1:
        return None
    raw = _mapping(item.get("raw"))
    if raw.get("encoding") == "base64":
        return matches[0], ""
    return matches[0], _evidence_text(raw)


def report_documents(state) -> list[dict]:
    documents = []
    for obj in state.role_data.get("objects", {}).values():
        if not isinstance(obj, dict) or obj.get("kind") != "report":
            continue
        artifact = _artifact(obj.get("ref"))
        payload = _mapping(obj.get("payload"))
        document = payload.get("document")
        if artifact is None or not isinstance(document, str):
            continue
        documents.append({**artifact, "document": document})
    return documents


def select_report(state, version: str | None) -> dict | None:
    documents = report_documents(state)
    current = state.artifacts.get("report")
    dumped = current.model_dump() if current is not None else None
    if version:
        matches = [item for item in documents if item["version"] == version]
    elif dumped:
        matches = [
            item
            for item in documents
            if item["artifact_id"] == dumped["artifact_id"]
            and item["version"] == dumped["version"]
            and item["content_hash"] == dumped["content_hash"]
        ]
    else:
        # Invalidated reports remain readable by explicit historical version, but
        # must never be presented as the current report (including over SSE).
        matches = []
    if len(matches) != 1:
        return None
    chosen = matches[0]
    current = state.artifacts.get("report")
    frozen = (
        current is not None
        and state.approved.get("report") == current
        and (
            chosen["artifact_id"] == current.artifact_id
            and chosen["version"] == current.version
        )
    )
    return {**chosen, "frozen": frozen}


def _workflow(payload) -> dict:
    raw = payload.get("workflow") if isinstance(payload, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "version": raw.get("version"),
        "phase": raw.get("phase"),
        "next_action": raw.get("next_action"),
        "role": raw.get("role"),
        "reason": raw.get("reason"),
    }


def _actions(
    *,
    record,
    busy: bool,
    recoverable: bool,
    interrupts: list[dict],
    signed: bool,
    correction_id=None,
    queue_blocked=False,
):
    terminal = bool(record.cancel_requested or record.status == "cancelled" or signed)
    actions = []
    if not terminal:
        actions.append(
            {
                "key": "cancel",
                "label": "取消",
                "description": "请求取消。已取消的会话不能再恢复。",
                "kind": "danger",
                "requires_confirmation": True,
            }
        )
    if (
        not terminal
        and not queue_blocked
        and not busy
        and not interrupts
        and (
            (record.run_id and (recoverable or record.status == "failed"))
            or (record.status == "created" and not record.run_id)
        )
    ):
        actions.append(
            {
                "key": "correct" if correction_id else "resume",
                **({"dispatch_call_id": correction_id} if correction_id else {}),
                "label": "纠正采集结果" if correction_id else "继续",
                "description": "确认用原有来源及剩余预算重新提交一次采集结果；可能产生模型费用。"
                if correction_id
                else "从检查点继续未完成的步骤，不重新批准已暂停的关口。",
                "kind": "primary",
                "requires_confirmation": True,
            }
        )
    return actions


def _shown_status(record, *, busy: bool, interrupts: list[dict]) -> str:
    if record.cancel_requested:
        return "cancelled"
    if busy:
        return "running"
    if interrupts:
        return "paused"
    return record.status


async def build_workspace(backend, report_id: str) -> dict:
    actor_id = backend.actor_id
    view = await backend.store.read_snapshot(report_id, actor_id)
    record = view.record
    from research_copilot.runtime.autonomy import autonomous, progress
    from research_copilot.runtime.source_contract import source_contract_compatible

    factory = getattr(backend.session, "agent_factory", None)
    contract_current = source_contract_compatible(
        view.state, getattr(factory, "source_contract", {}) or {}
    )
    read_only = (
        getattr(backend, "history_only", False)
        or not autonomous(view.state)
        or not contract_current
    )
    todos = view.state.role_data.get("todos", [])
    reader = getattr(
        getattr(backend.session, "agent_factory", None), "planning_state", None
    )
    if not read_only and reader:
        todos = await reader(record)
    queued = await backend.runs.state(report_id)
    queue_state = queued["status"] if queued else None
    busy = queue_state in {"pending", "claimed", "running"}
    retrospective = None
    if getattr(backend, "experience", None) is not None:
        try:
            retrospective = await backend.experience.summary(report_id)
        except LookupError:
            retrospective = None
        except Exception:  # noqa: BLE001 - retrospective must not break the report view
            logger.exception("retrospective summary failed")
            retrospective = None
    recoverable = (
        record.status == "running"
        and not busy
        and not record.cancel_requested
        and queue_state != "recovery_required"
    )
    context = RunContext(
        record.actor_id,
        record.report_id,
        record.run_id,
        record.budget_id,
        record.policy_version,
    )
    interrupts: list[dict] = []
    correction_id = None
    error_detail = None
    if record.run_id and not busy and not read_only:
        try:
            snapshot = await backend.session.get_status(report_id, actor_id)
            correction_id = getattr(snapshot, "correction_id", None)
            interrupts = normalize_interrupts(snapshot.interrupts)
            error_detail = getattr(snapshot, "error_detail", None)
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001 - retain the read-only durable projection if graph inspection fails
            backend.runs.errors.setdefault(report_id, type(exc).__name__)
    observability = None
    business_status = {}
    state = view.state
    if record.run_id or record.status:
        try:
            observability = await status_payload(
                backend.store, actor_id, report_id, snapshot=view
            )
        except (LookupError, PermissionError):
            observability = None
        business_status = BusinessCoordinator.project_status(
            context, state, view.budget.status()
        )
    signed = bool(observability and observability.get("signed"))
    shown = _shown_status(record, busy=busy, interrupts=interrupts)
    if read_only:
        shown = "read_only"
    review_raw = (observability or {}).get("review") if observability else None
    issues = []
    if isinstance(review_raw, dict):
        issues = [
            _issue(issue)
            for issue in review_raw.get("issues") or []
            if isinstance(issue, dict)
        ]
    document = select_report(state, None) if state is not None else None
    messages = (
        list(state.role_data.get("conversation", [])) if state is not None else []
    )
    if (
        not any(
            m.get("role") == "user" and m.get("content") == record.user_request
            for m in messages
        )
        and record.user_request
    ):
        messages.insert(
            0,
            {
                "id": "initial:" + record.report_id,
                "role": "user",
                "content": record.user_request,
                "kind": "text",
                "created_at": record.created_at,
            },
        )
    saved_error = state.role_data.get("last_run_error", {}) if state is not None else {}
    activities = []
    if state is not None:
        for audit in state.role_data.get("model_audit", [])[-8:]:
            activities.append(
                {
                    "id": audit.get("request_id"),
                    "role": audit.get("role") or audit.get("stage") or "supervisor",
                    "event": audit.get("event"),
                }
            )
    return {
        "workflow_version": state.role_data.get("workflow_version"),
        "read_only": read_only,
        "report_scope": state.role_data.get("report_scope"),
        "todos": todos,
        **progress(state),
        "waiting_for": (
            "clarification"
            if any(i["kind"] == "clarification" for i in interrupts)
            else "reviewer_deep_review"
            if any(i["kind"] == "reviewer_deep_review" for i in interrupts)
            else "signoff"
            if any(i.get("tool") == "signoff_report" for i in interrupts)
            else "recovery"
            if shown == "failed" or queue_state == "recovery_required"
            else None
        ),
        "report": {
            "id": record.report_id,
            "user_request": record.user_request,
            "status": shown,
            "created_at": record.created_at or None,
        },
        "runtime": {
            "busy": busy,
            "queue_state": queue_state,
            "queue_job_id": queued["id"] if queued else None,
            "recoverable": recoverable,
            "last_error": (queued.get("error") if queued else None)
            or backend.runs.errors.get(report_id),
            "error_detail": (
                "执行所有权已失效，原请求结果待核查；请先处理运行恢复。"
                if queue_state == "recovery_required"
                else error_detail or saved_error.get("message")
            ),
        },
        "messages": messages,
        "activity": activities,
        "workflow": _workflow(business_status),
        "budget": _budget((observability or {}).get("budget")),
        "signoff_ready": bool(observability and observability.get("signoff_ready")),
        "signed": signed,
        "plan": project_plan(state) if state is not None else None,
        "analysis": project_analysis(state) if state is not None else None,
        "review": None
        if not isinstance(review_raw, dict)
        else {
            "outcome": review_raw.get("outcome"),
            "checks_complete": bool(review_raw.get("checks_complete")),
            "pass_confidence": review_raw.get("pass_confidence"),
            "issues": issues,
        },
        "rework": project_rework((observability or {}).get("rework")),
        "retrospective": retrospective,
        "evidence_count": len(state.role_data.get("evidence", {}))
        if state is not None
        else 0,
        "report_document": None
        if document is None
        else {"version": document["version"], "frozen": document["frozen"]},
        "interrupts": interrupts,
        "actions": []
        if read_only
        else _actions(
            record=record,
            busy=busy,
            recoverable=recoverable,
            interrupts=interrupts,
            signed=signed,
            correction_id=correction_id,
            queue_blocked=queue_state == "recovery_required",
        ),
    }


def report_summary(record, *, active: bool = False) -> dict:
    version = 0
    return {
        "id": record.report_id,
        "status": "running" if active else record.status,
        "topic": None,
        "report_type": None,
        "report_skill": None,
        "current_version": version,
        "user_request": record.user_request,
        "created_at": record.created_at or None,
        "updated_at": record.created_at or None,
    }
