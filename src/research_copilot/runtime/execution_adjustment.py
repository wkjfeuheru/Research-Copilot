"""Host-only audited extension of a work item's sandbox attempts."""

from datetime import datetime, timezone
from uuid import uuid4

from research_copilot.contracts import RunContext
from research_copilot.runtime.work_group import work_group_id
from research_copilot.sandbox.execution_policy import work_key


def _granted_limit(group, grants):
    limit = 0
    for grant in grants:
        if (grant.get("plan"), grant.get("rework")) != (group, None):
            continue
        prior = grant.get("prior_execution_ids") or []
        additional = grant.get("additional_attempts")
        if prior and type(additional) is int and additional > 0:
            limit = max(limit, len(prior) + additional)
    return limit


async def extend_executions(store, *, report_id, actor_id, review_id, additional, note):
    """Add audited sandbox attempts to one work group; attempts are never erased."""
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Operator execution adjustment requires a nonempty note")
    if type(additional) is not int or not 0 < additional <= 20:
        raise ValueError("Additional attempts must be an integer between 1 and 20")
    async with store.lock(report_id):
        record = await store.load(report_id, actor_id)
        context = RunContext(
            actor_id, report_id, record.run_id, record.budget_id, record.policy_version
        )

        def operation(state):
            scope = state.artifacts.get("scope")
            if scope is None:
                raise ValueError("Report has no frozen research scope")
            group = work_group_id(scope, review_id)
            scopes = state.role_data.get("scopes", {})
            prior = sorted(
                execution_id
                for execution_id, execution in state.role_data.get(
                    "executions", {}
                ).items()
                if work_key(execution["dispatch_id"], scopes, state.dispatches)
                == (group, None)
            )
            if not prior:
                raise ValueError("No sandbox executions exist for this review round")
            grants = state.role_data.setdefault("execution_extensions", [])
            if len(prior) + additional <= _granted_limit(group, grants):
                raise ValueError("Sandbox attempt limit can only be extended")
            receipt_id = "operator:execution:" + uuid4().hex
            grant = {
                "receipt_id": receipt_id,
                "plan": group,
                "rework": None,
                "prior_execution_ids": prior,
                "additional_attempts": additional,
                "actor_id": actor_id,
                "reason": note,
                "authorized_at": datetime.now(timezone.utc).isoformat(),
            }
            repaired = []
            for ident, failure in state.role_data.get("dispatch_failures", {}).items():
                dispatch = state.dispatches.get(ident) or {}
                if (
                    failure.get("code")
                    in {"sandbox_execution_limit", "sandbox_deadline_exhausted"}
                    and dispatch.get("work_group") == group
                    and not failure.get("repaired_by")
                ):
                    failure["repaired_by"] = receipt_id
                    repaired.append(ident)
            grant["repaired_dispatches"] = repaired
            grants.append(grant)
            return {**grant, "previous_limit": len(prior)}

        return await store.mutate_control(context, operation)
