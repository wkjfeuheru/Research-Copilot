"""Read current authorized collection outcomes, including pre-analysis gaps."""

from research_copilot.contracts import TaskBrief
from research_copilot.runtime.rework import validate_dispatch
from research_copilot.subagents.contracts import SearchResult
from research_copilot.subagents.facts import validate_facts
from research_copilot.subagents.task_authorization import validate_task_contract


def collection_projection(state):
    plan = state.artifacts.get("plan")
    if not plan or state.approved.get("plan") != plan:
        return {}
    data, candidates = state.role_data, {}
    retired = {
        recovery["original_dispatch_id"]
        for recovery in data.get("dispatch_recoveries", {}).values()
        if recovery.get("plan") == plan.model_dump()
        and recovery.get("authorization_id")
        and recovery.get("target_dispatch_id")
        and recovery.get("original_dispatch_id")
    }
    for ident, binding in state.dispatches.items():
        if binding.get("role") != "searcher" or ident in retired:
            continue
        scope = data.get("scopes", {}).get(ident, {})
        try:
            brief = TaskBrief.model_validate(scope.get("task_brief"))
            validate_task_contract(state, binding["inputs"].get("plan"), brief)
            validate_dispatch(state, binding)
        except (ValueError, PermissionError, KeyError):
            continue
        if brief.role == "searcher":
            # Include pending attempts: a new unaccepted attempt must not silently
            # select an older accepted outcome. No ordering guess from JSON keys.
            candidates.setdefault(brief.task_id, []).append((ident, scope, brief))
    output = {}
    for task_id, attempts in candidates.items():
        if len(attempts) != 1:
            continue
        ident, scope, brief = attempts[0]
        accepted = state.results.get(ident, {})
        if accepted.get("role") != "searcher" or accepted.get("status") not in {
            "completed",
            "needs_input",
        }:
            continue
        try:
            result = SearchResult.model_validate(
                data.get("task_results", {}).get(ident)
            )
            if result.task_id != task_id or accepted.get("gaps") != [
                f"{gap.description}: {gap.impact}" for gap in result.gaps
            ]:
                continue
            if not set(result.evidence_ids) <= set(scope.get("evidence_ids", [])):
                continue
            facts = validate_facts(result, state, scope)
        except (ValueError, PermissionError, KeyError):
            continue
        output[task_id] = {
            "task_id": task_id,
            "dispatch_id": ident,
            "status": result.status,
            "accepted_status": accepted["status"],
            "gaps": [gap.model_dump() for gap in result.gaps],
            "source_warnings": result.source_warnings,
            "stop_reason": result.stop_reason,
            "facts": facts,
            "missing_required": sorted(
                set(brief.required_fields)
                - {fact["input_ref"]["key"] for fact in facts}
            ),
        }
    return output
