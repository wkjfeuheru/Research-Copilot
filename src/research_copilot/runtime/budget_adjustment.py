"""Host-only audited budget extension; never sends a model request."""

from research_copilot.contracts import RunContext


async def extend_stage_budget(
    store, *, report_id, actor_id, stage, tokens=None, calls=None, note,
    branch=None, branch_calls=None,
):
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Operator budget adjustment requires a nonempty note")
    if tokens is None and calls is None and branch_calls is None:
        raise ValueError("Stage adjustment requires a token or call limit")
    if (branch is None) != (branch_calls is None):
        raise ValueError("Branch adjustment requires both branch and branch_calls")
    async with store.lock(report_id):
        record = await store.load(report_id, actor_id)
        context = RunContext(
            actor_id, report_id, record.run_id, record.budget_id, record.policy_version
        )
        return await store.extend_stage_budget(
            context,
            stage=stage,
            tokens=tokens,
            calls=calls,
            actor_id=actor_id,
            note=note,
            branch=branch,
            branch_calls=branch_calls,
        )


async def extend_global_budget(store, *, report_id, actor_id, tokens=None, calls=None, note):
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Operator budget adjustment requires a nonempty note")
    if tokens is None and calls is None:
        raise ValueError("Global adjustment requires a token or call limit")
    async with store.lock(report_id):
        record = await store.load(report_id, actor_id)
        context = RunContext(
            actor_id, report_id, record.run_id, record.budget_id, record.policy_version
        )
        return await store.extend_global_budget(
            context,
            tokens=tokens,
            calls=calls,
            actor_id=actor_id,
            note=note,
        )
