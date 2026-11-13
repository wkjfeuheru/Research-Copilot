"""Authenticated read-only observability, independent of model construction."""

from fastapi import APIRouter, Depends, HTTPException
from research_copilot.runtime.rework import status as rework_status

from research_copilot.runtime.identity import RUNTIME_VERSION
from research_copilot.runtime.diagnostics import diagnostics


async def status_payload(store, actor_id, report_id, *, snapshot=None):
    snapshot = snapshot or await store.read_snapshot(report_id, actor_id)
    record, state, budget = snapshot.record, snapshot.state, snapshot.budget
    if (
        record.actor_id != actor_id
        or record.report_id != report_id
        or record.runtime_version != RUNTIME_VERSION
    ):
        raise PermissionError("Report unavailable")
    ref = state.artifacts.get("report")
    from research_copilot.runtime.signoff import current_review, signoff_error

    review = current_review(state)
    signoff_ready = signoff_error(state, budget.status()) is None
    from research_copilot.runtime.run_queue import RunQueue

    command = await RunQueue(store.engine).latest(report_id, actor_id)
    return {
        "command": None
        if command is None
        else {
            key: command[key] for key in ("id", "kind", "status", "error", "worker_id")
        },
        "signoff_ready": signoff_ready,
        "report_id": report_id,
        "runtime_version": RUNTIME_VERSION,
        "session_status": record.status,
        "artifacts": {k: v.model_dump() for k, v in state.artifacts.items()},
        "approved_versions": {k: v.model_dump() for k, v in state.approved.items()},
        "signed": ref is not None and state.approved.get("report") == ref,
        "rework": rework_status(state),
        "budget": budget.status(),
        "diagnostics": diagnostics(state, budget),
        "review": None
        if review is None
        else {
            k: review.get(k)
            for k in (
                "review_id",
                "reviewed",
                "outcome",
                "checks_complete",
                "pass_confidence",
                "issues",
            )
        },
    }


def create_status_router(store, actor_dependency):
    router = APIRouter()

    @router.get("/api/reports/{report_id}/observability")
    async def status(report_id: str, actor_id: str = Depends(actor_dependency)):
        if not isinstance(actor_id, str) or not actor_id:
            raise HTTPException(401, "Authentication required")
        try:
            return await status_payload(store, actor_id, report_id)
        except (LookupError, PermissionError):
            raise HTTPException(404, "Report unavailable") from None

    return router
