"""Pure product workflow decisions; no model, I/O or approval side effects."""

import hashlib
import json
import re

from research_copilot.contracts import TaskBrief, ROLE_NAMES

from research_copilot.runtime.identity import dispatch_identity

VERSION = "products-v1"


class WorkflowBlocked(RuntimeError):
    pass


class CollectionInputConflict(ValueError):
    """A derived value was required as a raw collection input."""


# Computed concepts belong to analysist required_metrics, never to raw collection.
_DERIVED_TOKENS = frozenset({"growth", "ratio", "qoq", "yoy", "pp"})
_DERIVED_TERMS = ("毛利率", "净利率", "增速", "环比", "同比", "差额")
# 原始披露行也含派生词：现金流量表的 change_in_*、融资融券/保证金 margin_*。
_RAW_MOVEMENT = ("change_in_", "_change_in_")
_RAW_MARGIN_TOKENS = frozenset({"trading", "financing", "pledge", "deposit", "balance"})


def derived_collection_fields(brief):
    """Return required_fields that are computed rather than directly disclosed."""
    derived = []
    for field in brief.required_fields:
        normalized = field.lower()
        tokens = set(re.split(r"[-_]", normalized))
        is_derived = "gross_profit" in normalized or bool(tokens & _DERIVED_TOKENS)
        if not is_derived and "margin" in tokens:
            is_derived = not (tokens & _RAW_MARGIN_TOKENS)
        if not is_derived and "change" in tokens:
            is_derived = not any(part in normalized for part in _RAW_MOVEMENT)
        if not is_derived and any(term in field for term in _DERIVED_TERMS):
            is_derived = True
        if is_derived:
            derived.append(field)
    return sorted(derived)


def dispatch_id(report_id, call_id):
    return dispatch_identity(report_id, call_id)


def correction_candidate(state, report_id, messages):
    """The current receipt-less product dispatch can be corrected explicitly."""
    from langchain_core.messages import ToolMessage

    try:
        action = decide_next(state, report_id)
    except WorkflowBlocked:
        return None
    if action.get("args", {}).get("subagent_type") not in {
        "searcher",
        "writer",
        "reviewer",
    }:
        return None
    call_id = action["id"]
    previous = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
    if (
        previous is None
        or previous.tool_call_id != call_id
        or previous.status != "error"
    ):
        return None
    try:
        error = json.loads(previous.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(error, dict):
        return None
    if error.get("dispatch_id") != dispatch_id(report_id, call_id):
        return None
    accepted = {
        "invalid_subagent_result",
        "role_result_repair_exhausted",
        "stage_budget_exhausted",
    }
    if error.get("error") not in accepted:
        return None
    corrected = any(
        command("task", {}, [original, receipt["authorization_id"]])["id"] == call_id
        for original, receipt in state.role_data.get("dispatch_recoveries", {}).items()
    )
    # A correction already used its one retry unless an explicit stage-budget
    # adjustment authorizes another dispatch.
    if corrected and error.get("error") != "stage_budget_exhausted":
        return None
    return call_id


def validate_product_plan(tasks):
    briefs = [TaskBrief.model_validate(t) for t in tasks]
    if len({t.task_id for t in briefs}) != len(briefs):
        raise ValueError("Product task IDs must be unique")
    if len({t.as_of for t in briefs}) != 1:
        raise ValueError("Product tasks require one research as_of")
    # Collection may be split into bounded batches; downstream products aggregate them.
    if set(t.role for t in briefs) != set(ROLE_NAMES) or any(
        sum(t.role == role for t in briefs) != 1 for role in ROLE_NAMES[1:]
    ):
        raise ValueError(
            "Plan requires collection and one analysis, report and review product"
        )
    seen_fields = set()
    for task in (t for t in briefs if t.role == "searcher"):
        if derived := derived_collection_fields(task):
            raise CollectionInputConflict(
                "Searcher required_fields must be raw disclosed inputs; computed "
                "values belong to analysist required_metrics: " + ", ".join(derived)
            )
        fields = set(task.required_fields)
        overlap = seen_fields & fields
        if overlap:
            raise ValueError(
                "Searcher tasks must not require the same field; merge collection "
                "for one disclosure into one task: " + ", ".join(sorted(overlap))
            )
        seen_fields |= fields
    return briefs


def command(name, args, identity):
    ident = hashlib.sha256(
        json.dumps([name, identity], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:32]
    return {"kind": "tool", "name": name, "args": args, "id": "wf-" + ident}


def decide_next(state, report_id):
    def gate(kind, action):
        ref = state.artifacts.get(kind)
        if ref is None:
            raise WorkflowBlocked(f"Missing required {kind} product")
        if state.approved.get(kind) != ref:
            return command(action, {"artifact": ref.model_dump()}, ref.model_dump())
        return None

    if (
        state.artifacts.get("report")
        and state.approved.get("report") == state.artifacts["report"]
    ):
        return {"kind": "complete"}
    if any(
        r["status"] in {"started", "unknown", "output_ready"}
        for r in state.role_data.get("executions", {}).values()
    ):
        raise WorkflowBlocked(
            "recovery_required: existing execution outcome must be resolved"
        )
    plan = state.artifacts.get("plan")
    if plan is None:
        return {"kind": "plan", "tools": ["propose_plan"]}
    entries = [p for p in state.plans if p["artifact"] == plan.model_dump()]
    if len(entries) != 1:
        raise WorkflowBlocked("Plan version is missing or ambiguous")
    tasks = validate_product_plan(entries[0]["plan"]["tasks"])
    if action := gate("plan", "confirm_plan"):
        return action
    rework = state.artifacts.get("rework")
    start = 0
    payload = {}
    if rework:
        payload = state.role_data.get("rework", {}).get(rework.artifact_id, {})
        if not payload.get("tasks"):
            return {"kind": "plan", "tools": ["propose_rework"]}
        if action := gate("rework", "confirm_rework"):
            return action
        start = min(ROLE_NAMES.index(t["role"]) for t in payload["tasks"])
    generation = [plan.model_dump(), rework.model_dump() if rework else None]
    for role in ROLE_NAMES[start:]:
        if role == "writer":
            if action := gate("analysis", "confirm_analysis"):
                return action
        if role == "reviewer" and "report" not in state.artifacts:
            raise WorkflowBlocked("Missing report after writing")
        for task in (t for t in tasks if t.role == role):
            brief = task.model_dump()
            fixes = [t for t in payload.get("tasks", []) if t["role"] == role]
            if fixes:
                brief["objective"] += "\nApproved rework: " + json.dumps(
                    fixes, ensure_ascii=False
                )
                brief["feedback_refs"] = list(
                    dict.fromkeys(brief["feedback_refs"] + [payload["review_id"]])
                )
            action = command(
                "task",
                {
                    "subagent_type": role,
                    "description": json.dumps(brief, ensure_ascii=False),
                },
                [generation, task.task_id],
            )
            result = state.results.get(dispatch_id(report_id, action["id"]))
            # Only explicit, persisted operator authorizations create another
            # attempt. Follow the recovery chain so a second authorized
            # correction is not shadowed by the first failed identity.
            for _ in range(4):
                if result is not None:
                    break
                recovery = state.role_data.get("dispatch_recoveries", {}).get(
                    action["id"]
                )
                if not recovery:
                    break
                action = command(
                    "task",
                    action["args"],
                    [action["id"], recovery["authorization_id"]],
                )
                result = state.results.get(dispatch_id(report_id, action["id"]))
            if result is None:
                return action
            if role == "reviewer":
                if result["status"] == "changes_requested":
                    return {"kind": "plan", "tools": ["propose_rework"]}
                if (
                    result["status"] != "passed"
                    or state.reviewed_report != state.artifacts["report"]
                ):
                    raise WorkflowBlocked(
                        "Current report lacks a passing review receipt"
                    )
            elif result["status"] != "completed":
                raw = state.role_data.get("task_results", {}).get(
                    dispatch_id(report_id, action["id"]), {}
                )
                if (
                    role == "analysist"
                    and raw.get("status") == "partial"
                    and state.artifacts.get("analysis") is not None
                ):
                    # The analysis confirmation gate is the explicit operator
                    # decision point for an analysis that is complete but
                    # carries disclosed source/limitation gaps.
                    continue
                raise WorkflowBlocked(
                    f"{task.task_id}: {result['status']}; explicit scope decision required"
                )
    return gate("report", "signoff_report") or {"kind": "complete"}
