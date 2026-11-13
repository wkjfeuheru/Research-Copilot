"""Bind every dispatch to one current, human-approved task contract."""

from research_copilot.contracts import TaskBrief
from research_copilot.subagents.contracts import BusinessInputError


def validate_task_contract(state, plan_ref, brief):
    from research_copilot.runtime.autonomy import autonomous, validate_frozen_task

    if autonomous(state):
        validate_frozen_task(state, brief)
        return
    current = state.artifacts.get("plan")
    if (
        not current
        or state.approved.get("plan") != current
        or plan_ref != current.model_dump()
    ):
        raise BusinessInputError("Task requires the current approved plan")
    plans = [entry["plan"] for entry in state.plans if entry["artifact"] == plan_ref]
    tasks = [
        task
        for plan in plans
        for task in plan.get("tasks", [])
        if isinstance(task, dict) and task.get("task_id") == brief.task_id
    ]
    if len(plans) != 1 or len(tasks) != 1:
        raise BusinessInputError(
            "Task must uniquely match a unique task in the approved plan"
        )
    frozen, supplied = (
        TaskBrief.model_validate(tasks[0]).model_dump(),
        brief.model_dump(),
    )
    mismatches = [
        field
        for field in (
            "role",
            "as_of",
            "max_source_calls",
            "max_no_progress_calls",
            "max_facts",
            "max_executions",
            "problem_type",
            "execution_purposes",
        )
        if frozen[field] != supplied[field]
    ]
    for field in ("required_fields", "optional_fields"):
        if set(frozen[field]) != set(supplied[field]):
            mismatches.append(field)
    if any(
        metric not in supplied["required_metrics"]
        for metric in frozen["required_metrics"]
    ):
        mismatches.append("required_metrics")
    if mismatches:
        raise BusinessInputError(
            "TaskBrief deviates from the frozen plan task ("
            + ", ".join(sorted(mismatches))
            + "); dispatch the approved task contract or confirm a new plan/rework"
        )
