"""Validate reconciliation transitions against the locked durable ledger."""

from research_copilot.runtime.budget import BudgetExceeded, TokenBudget


async def validate_reconciliation(current, proposed):
    before, after = current.get("requests", {}), proposed.get("requests", {})
    changed = [
        key
        for key, request in before.items()
        if "usage" in request
        and any(
            request.get(field) != after.get(key, {}).get(field)
            for field in (
                "usage",
                "reconciled",
                "reconciliation",
                "details",
                "reconcile_details",
                "estimated_cost",
            )
        )
    ]
    if not changed and proposed["charged_tokens"] >= current["charged_tokens"]:
        return
    if len(changed) != 1 or changed[0] not in after:
        raise ValueError("Invalid budget reconciliation transition")
    key = changed[0]
    request = after[key]
    if not request.get("reconciled") or before[key].get("reconciled"):
        raise ValueError("Invalid budget reconciliation transition")
    # Recompute the complete transition, including cost and per-stage totals.
    # A flag on an arbitrary snapshot is not authorization to refund usage.
    replay = TokenBudget(**current)
    try:
        await replay.reconcile(
            key, request["usage"], details=request.get("reconcile_details")
        )
    except BudgetExceeded:
        # Over-budget reconciliation must still be durable before it is reported.
        pass
    if replay.snapshot() != proposed:
        raise ValueError("Invalid budget reconciliation totals or audit")
