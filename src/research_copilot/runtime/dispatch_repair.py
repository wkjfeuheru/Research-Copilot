"""Host-only audited repair of a dispatch's result-validation failure."""

from datetime import datetime, timezone
from uuid import uuid4

from research_copilot.contracts import RunContext

# Only the failure codes that no other lever can clear. Budget and sandbox
# codes are repaired by their matching budget/execution levers instead.
# invalid_subagent_result stays repairable because the recorded message now
# carries the concrete cause (defect ⑧), so an operator can judge a re-dispatch.
REPAIRABLE_CODES = {"role_result_repair_exhausted", "invalid_subagent_result"}


async def repair_dispatch_result(store, *, report_id, actor_id, dispatch_id, note):
    """Clear one result-validation failure for a narrowed re-dispatch.

    Usage, dispatch history and budgets are never reset; the repair is only
    meaningful together with a new task brief that fixes the concrete errors.
    """
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Operator dispatch repair requires a nonempty note")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("Operator dispatch repair requires an actor")
    async with store.lock(report_id):
        record = await store.load(report_id, actor_id)
        context = RunContext(
            actor_id, report_id, record.run_id, record.budget_id, record.policy_version
        )

        def operation(state):
            dispatch = state.dispatches.get(dispatch_id)
            if dispatch is None:
                raise ValueError("Unknown dispatch: " + str(dispatch_id))
            failure = state.role_data.get("dispatch_failures", {}).get(dispatch_id)
            if not failure:
                raise ValueError("Dispatch has no recorded failure")
            if failure.get("repaired_by"):
                raise ValueError("Dispatch failure is already repaired")
            if failure.get("code") not in REPAIRABLE_CODES:
                raise ValueError(
                    "Operator repair only covers subagent result failures ("
                    + ", ".join(sorted(REPAIRABLE_CODES))
                    + "); use the matching lever for "
                    + str(failure.get("code"))
                )
            receipt_id = "operator:repair:" + uuid4().hex
            failure["repaired_by"] = receipt_id
            entry = {
                "receipt_id": receipt_id,
                "dispatch_id": dispatch_id,
                "role": dispatch.get("role"),
                "failure_code": failure["code"],
                "actor_id": actor_id,
                "note": note,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
            state.role_data.setdefault("dispatch_repairs", []).append(entry)
            return entry

        return await store.mutate_control(context, operation)
