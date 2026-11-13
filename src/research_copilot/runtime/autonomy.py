"""Server-owned scope and atomic admission for the native planning workflow.

Todos are deliberately absent: model progress is never an authorization source.
All mutators here run inside ControlRepository.mutate_control.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from research_copilot.contracts import ArtifactRef, FIELD_KEY_PATTERN
from research_copilot.subagents.contracts import BusinessInputError

WORKFLOW_VERSION = "products-v2"
DISPATCH_LIMIT = 12

# objective 中「key/字段…（如 xxx、yyy）」式枚举：括号内的 ASCII 标识被视为
# 显式要求登记的事实 key，必须已进入冻结字段集（真实运行 WLY-20261107 的失败形态）。
_ENUMERATED_KEYS = re.compile(r"[（(]\s*如\s*[:：]?\s*([^（）()]+)[）)]")
_KEY_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def unregistered_enumerated_keys(description, fields):
    """Fact keys enumerated as examples must already be inside frozen fields."""
    missing = []
    for match in _ENUMERATED_KEYS.finditer(description):
        lead = description[max(0, match.start() - 12) : match.start()]
        if "key" not in lead and "字段" not in lead:
            continue
        for item in re.split(r"[、，,;/]", match.group(1)):
            candidate = item.strip()
            # 仅限定符形态的标识才算事实 key；中文说明或普通术语忽略。
            if not (_KEY_TOKEN.fullmatch(candidate) and "_" in candidate):
                continue
            if candidate not in fields and candidate not in missing:
                missing.append(candidate)
    return missing


def autonomous(state):
    return state.role_data.get("workflow_version") == WORKFLOW_VERSION


class ReportScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    report_type: Literal[
        "company_report", "flash_comment", "industry_report", "strategy_report"
    ]
    objective: str = Field(min_length=1, max_length=8000)
    as_of: str

    @field_validator("as_of")
    @classmethod
    def valid_as_of(cls, value):
        from research_copilot.subagents.source_policy import parse_research_as_of

        return parse_research_as_of(value).isoformat()


def freeze_scope(state, report_id, scope):
    if not autonomous(state):
        raise PermissionError("Report scope requires products-v2")
    scope = ReportScope.model_validate(scope)
    value = scope.model_dump()
    existing = state.role_data.get("report_scope")
    if existing and existing != value:
        raise ValueError("Report scope is frozen; start a new report to change it")
    if state.role_data.get("research_as_of", scope.as_of) != scope.as_of:
        raise ValueError("Research as_of is frozen")
    ref = ArtifactRef(
        artifact_id="scope:" + report_id,
        version="1",
        content_hash=hashlib.sha256(scope.model_dump_json().encode()).hexdigest(),
    )
    state.role_data["report_scope"] = value
    state.role_data["research_as_of"] = scope.as_of
    state.artifacts["scope"] = ref
    return ref


def report_policy(state):
    if autonomous(state):
        return state.role_data.get("report_scope", {})
    current = state.artifacts.get("plan")
    return next(
        (
            p["plan"]
            for p in state.plans
            if current and p["artifact"] == current.model_dump()
        ),
        {},
    )


def analysis_input(state):
    return (state.artifacts if autonomous(state) else state.approved).get("analysis")


def stop_reason(state):
    if not autonomous(state):
        return None
    for ident, failure in state.role_data.get("dispatch_failures", {}).items():
        if failure.get("repaired_by"):
            continue
        if not failure.get("repairable") or state.dispatches[ident].get("repair_of"):
            return "Automatic repair stopped; manual recovery required: " + str(
                failure.get("code", "unknown_result")
            )
    return None


def dispatch_limit(state):
    """Server base limit plus audited operator grants; grants can only raise it."""
    limit = DISPATCH_LIMIT
    for grant in state.role_data.get("dispatch_extensions", []):
        additional = grant.get("additional")
        if type(additional) is int and additional > 0:
            limit += additional
    return limit


def admit(state, dispatch, brief, description):
    """Freeze task + input versions + count together; replay never spends again."""
    data = state.role_data
    scope = data.get("report_scope")
    if not scope or "scope" not in state.artifacts:
        raise BusinessInputError("Set report scope before dispatch")
    if "report" in state.approved:
        raise PermissionError("Signed reports cannot dispatch")
    existing = state.dispatches.get(dispatch.dispatch_id)
    if existing:
        if (
            existing.get("description") != description
            or existing["role"] != dispatch.role
        ):
            raise ValueError("Replay dispatch contract changed")
        return
    # Admission-only validation keeps historical TaskBrief payloads readable.
    fields = brief.required_fields + brief.optional_fields
    invalid = [key for key in fields if not re.fullmatch(FIELD_KEY_PATTERN, key)]
    if invalid:
        raise BusinessInputError(
            f"Invalid field keys: {invalid}. Use stable ASCII keys such as revenue_2024; put Chinese labels in objective/acceptance_criteria."
        )
    if len(fields) != len(set(fields)):
        raise BusinessInputError("Duplicate required/optional field keys")
    if len(brief.required_fields) > brief.max_facts:
        raise BusinessInputError(
            "required_fields exceeds max_facts; narrow or split the task before dispatch"
        )
    if dispatch.role == "searcher":
        from research_copilot.runtime.workflow import derived_collection_fields

        if derived := derived_collection_fields(brief):
            raise BusinessInputError(
                "Searcher required_fields must be raw disclosed inputs; computed "
                "values belong to analysist required_metrics: " + ", ".join(derived)
            )
        missing = unregistered_enumerated_keys(description, set(fields))
        if missing:
            raise BusinessInputError(
                f"Objective enumerates fact keys outside frozen fields: {missing}. "
                f"Frozen fields: {sorted(fields)}. Add them to required_fields/"
                f"optional_fields within max_facts, or drop the registration "
                "requirement; the description itself is not evidence."
            )
    if dispatch.role == "analysist":
        if brief.calculation_mode == "declarative" and not brief.required_metrics:
            raise BusinessInputError(
                "Declarative analysis requires nonempty required_metrics (key, semantic_type, unit)"
            )
        facts = {
            fact["input_ref"]["key"]
            for sheet in data.get("fact_sheets", {}).values()
            if state.dispatches.get(sheet["dispatch_id"], {})
            .get("inputs", {})
            .get("scope")
            == state.artifacts["scope"].model_dump()
            for fact in sheet["facts"]
        }
        missing = set(brief.required_fields) - facts
        if missing or (brief.calculation_mode == "declarative" and not facts):
            raise BusinessInputError(
                f"Analysis requires validated facts; missing: {sorted(missing)}. Repair SearchResult.facts before dispatch; raw evidence alone is not a fact handoff."
            )
    if any(
        e.get("status") in {"started", "unknown", "output_ready"}
        for e in data.get("executions", {}).values()
    ):
        raise BusinessInputError(
            "Report contains an unknown execution; recover it before dispatch"
        )
    if reason := stop_reason(state):
        raise BusinessInputError(reason)
    failures = data.get("dispatch_failures", {})
    unresolved = {
        key: failure
        for key, failure in failures.items()
        if not failure.get("repaired_by")
        and state.dispatches[key]["role"] == dispatch.role
    }
    if unresolved:
        if set(unresolved) != {brief.repair_of}:
            raise BusinessInputError(
                "A failed task requires an explicit bounded repair"
            )
        original = state.dispatches[brief.repair_of]
        frozen = data["tasks"][original["task_id"]]["brief"]
        if any(
            getattr(brief, key) > frozen[key]
            for key in (
                "max_source_calls",
                "max_no_progress_calls",
                "max_facts",
                "max_executions",
            )
        ):
            raise BusinessInputError("Repair cannot increase frozen task allowances")
        if original.get("repair_of") or not unresolved[brief.repair_of].get(
            "repairable"
        ):
            raise BusinessInputError(
                "Automatic repair unavailable or exhausted; recover manually"
            )
    elif brief.repair_of:
        raise BusinessInputError("Unknown repair predecessor")
    if brief.as_of != scope["as_of"]:
        raise BusinessInputError("Task as_of differs from frozen report as_of")
    if brief.max_facts > 15 or brief.max_executions > 2:
        raise BusinessInputError("Task exceeds report facts/execution limit")
    if len(state.dispatches) >= dispatch_limit(state):
        raise BusinessInputError("Report dispatch limit exhausted")
    if dispatch.role == "writer" and analysis_input(state) is None:
        raise BusinessInputError("Writer requires current analysis")
    if dispatch.role == "reviewer" and "report" not in state.artifacts:
        raise BusinessInputError("Reviewer requires current report")
    allowed = set(data.get("evidence", {})) | {
        r.artifact_id for r in state.artifacts.values()
    }
    for evidence in data.get("evidence", {}).values():
        path = evidence.get("source", {}).get("arguments", {}).get("path")
        if path:
            allowed.add(path)
    # Material projections are immutable, host-registered paths, not arbitrary files.
    for material in data.get("scopes", {}).values():
        for path in material.get("paths", {}):
            if any(
                path.endswith("/" + ident + ".json")
                for ident in allowed
                if not ident.startswith("/")
            ):
                allowed.add(path)
    if not set(brief.input_refs) <= allowed:
        raise BusinessInputError("Unknown or stale task input reference")
    if not set(brief.feedback_refs) <= set(data.get("reviews", {})):
        raise BusinessInputError("Unknown review feedback reference")
    task = brief.model_dump()
    previous = data.get("tasks", {}).get(brief.task_id)
    if previous and previous["brief"] != task:
        raise BusinessInputError("Frozen task contract changed; use a new task version")
    if previous:
        raise BusinessInputError(
            "Task already dispatched; recover its existing attempt"
        )
    rework = state.artifacts.get("rework")
    payload = data.get("rework", {}).get(rework.artifact_id, {}) if rework else {}
    if dispatch.role == "writer" and any(
        t["role"] in {"searcher", "analysist"} for t in payload.get("tasks", [])
    ):
        if state.artifacts["analysis"].model_dump() == payload.get("base_analysis_ref"):
            raise BusinessInputError(
                "Upstream rework requires a new analysis before writing"
            )
    if dispatch.role == "reviewer" and payload.get("tasks"):
        if state.artifacts["report"].model_dump() == payload.get("base_report_ref"):
            raise BusinessInputError("Rework requires a new report before review")
    work_group = hashlib.sha256(
        json.dumps(
            [state.artifacts["scope"].model_dump(), payload.get("review_id")],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    binding = {
        "role": dispatch.role,
        "inputs": {k: r.model_dump() for k, r in state.artifacts.items()},
        "description": description,
        "work_group": work_group,
        "task_id": brief.task_id,
    }
    if brief.repair_of:
        original = state.dispatches[brief.repair_of]
        if original["work_group"] != work_group:
            raise BusinessInputError("Repair work group changed")
        binding["repair_of"] = brief.repair_of
    from research_copilot.runtime.rework import validate_dispatch

    validate_dispatch(state, binding)
    state.dispatches[dispatch.dispatch_id] = binding
    predecessor = (
        data.get("tasks", {}).get(original["task_id"]) if brief.repair_of else None
    )
    data.setdefault("tasks", {})[brief.task_id] = {
        "version": predecessor.get("version", 1) + 1 if predecessor else 1,
        "predecessor_task_id": original["task_id"] if predecessor else None,
        "brief": task,
        "dispatch_id": dispatch.dispatch_id,
        "work_group": work_group,
    }
    if brief.repair_of:
        failures[brief.repair_of]["repaired_by"] = dispatch.dispatch_id


def validate_frozen_task(state, brief):
    entry = state.role_data.get("tasks", {}).get(brief.task_id)
    if not entry or entry["brief"] != brief.model_dump():
        raise BusinessInputError("Task differs from server-frozen contract")


def progress(state):
    tasks = []
    for ident, entry in state.role_data.get("tasks", {}).items():
        dispatch_id = entry["dispatch_id"]
        result = state.results.get(dispatch_id)
        role = entry["brief"]["role"]
        status = (
            result.get("status")
            if result
            else "failed"
            if dispatch_id in state.role_data.get("dispatch_failures", {})
            or state.role_data.get("last_run_error")
            else "in_progress"
        )
        dependency = (
            "analysis" if role == "writer" else "report" if role == "reviewer" else None
        )
        if dependency and result:
            current = state.artifacts.get(dependency)
            if (
                not current
                or state.dispatches[dispatch_id]["inputs"].get(dependency)
                != current.model_dump()
            ):
                status = "stale"
            elif (
                role == "reviewer"
                and status == "passed"
                and state.reviewed_report != current
            ):
                status = "stale"
        tasks.append(
            {
                "task_id": ident,
                "dispatch_id": dispatch_id,
                "role": entry["brief"]["role"],
                "objective": entry["brief"]["objective"],
                "version": entry.get("version", 1),
                "predecessor_task_id": entry.get("predecessor_task_id"),
                "issue_ids": list(
                    dict.fromkeys(
                        issue
                        for task in state.role_data.get("rework", {})
                        .get(
                            state.dispatches[dispatch_id]
                            .get("inputs", {})
                            .get("rework", {})
                            .get("artifact_id"),
                            {},
                        )
                        .get("tasks", [])
                        if task["role"] == entry["brief"]["role"]
                        for issue in task["issue_ids"]
                    )
                ),
                "status": status,
            }
        )
    return {
        "tasks": tasks,
        "dispatch_count": len(state.dispatches),
        "dispatch_remaining": max(0, dispatch_limit(state) - len(state.dispatches)),
    }
