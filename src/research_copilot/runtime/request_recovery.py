"""Host-only audited request resolution; never sends a model request."""

from research_copilot.contracts import RunContext


async def recover_request(
    store,
    *,
    report_id,
    actor_id,
    request_id,
    note,
    usage=None,
    authorize_retry=False,
    provider_request_id=None,
    expected_revision=None,
    action=None,
):
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Operator recovery requires a nonempty note")
    async with store.lock(report_id):
        record = await store.load(report_id, actor_id)
        context = RunContext(
            actor_id, report_id, record.run_id, record.budget_id, record.policy_version
        )
        budget = await store.load_budget(context)
        before = budget.status()
        if expected_revision is not None and budget.revision != expected_revision:
            from research_copilot.runtime.run_queue import QueueConflict

            raise QueueConflict("Budget changed; refresh the recovery record")
        if request_id not in budget.requests:
            raise ValueError("Unknown reservation ID")
        pending = request_id in budget.snapshot()["pending"]
        if action == "resolve_pending" and not pending:
            raise ValueError("Request is no longer pending")
        if action == "reconcile" and (pending or usage is None):
            raise ValueError(
                "Reconciliation requires a settled request and verified usage"
            )
        if action == "authorize_retry" and (
            pending or not budget.requests[request_id].get("operation_id")
        ):
            raise ValueError(
                "Retry requires a settled request with a stable operation identity"
            )
        details = {"source": "operator_recovery", "actor_id": actor_id, "note": note}
        if provider_request_id:
            details["provider_request_id"] = provider_request_id
        if pending:
            await budget.recover_pending(
                request_id, actor_id=actor_id, note=note, usage=usage, details=details
            )
        elif usage is not None:
            await budget.reconcile(request_id, usage, details=details)
        if authorize_retry:
            await budget.authorize_retry(request_id, actor_id=actor_id, note=note)
        return {
            "before": before,
            "after": budget.status(),
            "request": budget.requests[request_id],
        }
