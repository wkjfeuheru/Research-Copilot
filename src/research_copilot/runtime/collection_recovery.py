"""Host-only audited recovery of collection no-progress counters."""

from datetime import datetime, timezone
from uuid import uuid4

from research_copilot.contracts import RunContext
from research_copilot.runtime.work_group import work_group_id as _work_group


async def recover_collection(store, *, report_id, actor_id, review_id, note):
    """Reset one review round's no-progress block; never relaxes call/fact limits."""
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Operator collection recovery requires a nonempty note")
    async with store.lock(report_id):
        record = await store.load(report_id, actor_id)
        context = RunContext(
            actor_id, report_id, record.run_id, record.budget_id, record.policy_version
        )

        def operation(state):
            scope = state.artifacts.get("scope")
            if scope is None:
                raise ValueError("Report has no frozen research scope")
            work_group = _work_group(scope, review_id)
            work = state.role_data.get("collection_work", {}).get(work_group)
            if work is None:
                raise ValueError(
                    "No collection work group exists for this review round"
                )
            previous = work.get("no_progress_calls", 0)
            blocked_scopes = []
            for ident, binding in state.dispatches.items():
                if binding.get("work_group") != work_group:
                    continue
                material = state.role_data.get("scopes", {}).get(ident)
                if material and material.get("collection_stop_reason") == "no_progress":
                    material.pop("collection_stop_reason", None)
                if material and material.get("no_progress_calls", 0):
                    material["no_progress_calls"] = 0
                    blocked_scopes.append(ident)
            previous_stop = work.pop("stop_reason", None)
            if not previous and not previous_stop and not blocked_scopes:
                return {
                    "work_group": work_group,
                    "review_id": review_id,
                    "changed": False,
                    "no_progress_calls": 0,
                }
            work["no_progress_calls"] = 0
            receipt = {
                "receipt_id": "operator:collection:" + uuid4().hex,
                "work_group": work_group,
                "review_id": review_id,
                "previous_no_progress_calls": previous,
                "reset_dispatches": sorted(blocked_scopes),
                "actor_id": actor_id,
                "note": note,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "changed": True,
            }
            state.role_data.setdefault("collection_recoveries", []).append(receipt)
            return receipt

        return await store.mutate_control(context, operation)
