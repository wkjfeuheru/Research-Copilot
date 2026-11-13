"""Host-only audited extension of a report's dispatch quota."""

from datetime import datetime, timezone
from uuid import uuid4

from research_copilot.contracts import RunContext
from research_copilot.runtime.autonomy import DISPATCH_LIMIT


def _granted_additional(grants):
    total = 0
    for grant in grants:
        additional = grant.get("additional")
        if type(additional) is int and additional > 0:
            total += additional
    return total


async def extend_dispatches(store, *, report_id, actor_id, additional, note):
    """Add audited dispatch quota to one report; prior dispatches are never erased."""
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Operator dispatch adjustment requires a nonempty note")
    if type(additional) is not int or not 0 < additional <= 12:
        raise ValueError("Additional dispatches must be an integer between 1 and 12")
    async with store.lock(report_id):
        record = await store.load(report_id, actor_id)
        context = RunContext(
            actor_id, report_id, record.run_id, record.budget_id, record.policy_version
        )

        def operation(state):
            grants = state.role_data.setdefault("dispatch_extensions", [])
            limit = DISPATCH_LIMIT + _granted_additional(grants)
            if len(state.dispatches) + additional <= limit:
                raise ValueError("Report dispatch limit can only be extended")
            grant = {
                "receipt_id": "operator:dispatch:" + uuid4().hex,
                "previous_limit": limit,
                "additional": additional,
                "actor_id": actor_id,
                "reason": note,
                "authorized_at": datetime.now(timezone.utc).isoformat(),
            }
            grants.append(grant)
            return grant

        return await store.mutate_control(context, operation)
