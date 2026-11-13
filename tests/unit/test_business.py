from copy import deepcopy

import pytest

from tests.unit.test_orchestration import context


def _brief(task_id, role="searcher"):
    from research_copilot.contracts import TaskBrief

    return TaskBrief(task_id=task_id, role=role, objective=task_id, as_of="2025-04-30")


class ControlRepository:
    def __init__(self):
        from research_copilot.runtime.business import ControlState

        self.state = ControlState()

    async def read_control(self, context):
        return deepcopy(self.state)

    async def mutate_control(self, context, operation, *, include_budget=False):
        state = deepcopy(self.state)
        result = (
            operation(state, {"usage_verified": True})
            if include_budget
            else operation(state)
        )
        self.state = state
        return result


@pytest.mark.asyncio
async def test_unapproved_plan_blocks_dispatch_and_confirmation_cannot_bypass_human_gate():
    from research_copilot.contracts import Dispatch
    from research_copilot.runtime.business import (
        BusinessCoordinator,
        ResearchPlan,
        approval_key,
    )

    business = BusinessCoordinator(ControlRepository())
    plan = await business.propose_plan(
        context(),
        ResearchPlan(
            report_type="company_report",
            objective="Research company",
            tasks=[
                _brief("collect"),
                _brief("analyze", "analysist"),
                _brief("write", "writer"),
            ],
        ),
    )
    with pytest.raises(PermissionError, match="plan"):
        await business.authorize_dispatch(
            context(), Dispatch("d", "searcher", "/workspace/tasks/d/")
        )
    key = approval_key(context().report_id, "confirm_plan", plan)
    with pytest.raises(PermissionError, match="human"):
        await business.confirm(context(), "confirm_plan", plan, key)
    pending = {
        "interrupt-1": {
            "action_requests": [
                {"name": "confirm_plan", "args": {"artifact": plan.model_dump()}}
            ]
        }
    }
    await business.validate_resume(
        context(), pending, {"interrupt-1": {"decisions": [{"type": "approve"}]}}
    )
    first = await business.confirm(context(), "confirm_plan", plan, key)
    second = await business.confirm(context(), "confirm_plan", plan, key)
    assert first == second
    await business.authorize_dispatch(
        context(), Dispatch("d", "searcher", "/workspace/tasks/d/")
    )


@pytest.mark.asyncio
async def test_modified_plan_invalidates_pending_approval_and_signoff_needs_current_review():
    from research_copilot.contracts import ArtifactRef
    from research_copilot.runtime.business import (
        BusinessCoordinator,
        ResearchPlan,
        approval_key,
    )

    repository = ControlRepository()
    business = BusinessCoordinator(repository)
    plan = await business.propose_plan(
        context(),
        ResearchPlan(
            report_type="flash_comment", objective="old", tasks=[_brief("collect")]
        ),
    )
    pending = {
        "i": {
            "action_requests": [
                {"name": "confirm_plan", "args": {"artifact": plan.model_dump()}}
            ]
        }
    }
    await business.propose_plan(
        context(),
        ResearchPlan(
            report_type="flash_comment", objective="changed", tasks=[_brief("collect")]
        ),
    )
    with pytest.raises(ValueError, match="stale"):
        await business.validate_resume(
            context(), pending, {"i": {"decisions": [{"type": "approve"}]}}
        )
    draft = ArtifactRef(artifact_id="draft", version="1", content_hash="a" * 64)
    repository.state.artifacts["report"] = draft
    repository.state.grants[approval_key("report-1", "signoff_report", draft)] = (
        "actor-1"
    )
    with pytest.raises(PermissionError, match="review"):
        await business.confirm(
            context(),
            "signoff_report",
            draft,
            approval_key("report-1", "signoff_report", draft),
        )


@pytest.mark.asyncio
async def test_writer_requires_confirmed_analysis_even_with_confirmed_plan():
    from research_copilot.contracts import ArtifactRef, Dispatch
    from research_copilot.runtime.business import BusinessCoordinator

    repository = ControlRepository()
    plan = ArtifactRef(artifact_id="plan", version="1", content_hash="b" * 64)
    repository.state.artifacts["plan"] = plan
    repository.state.approved["plan"] = plan
    business = BusinessCoordinator(repository)
    with pytest.raises(PermissionError, match="analysis"):
        await business.authorize_dispatch(
            context(), Dispatch("d", "writer", "/workspace/tasks/d/")
        )


@pytest.mark.asyncio
async def test_review_pass_is_bound_to_the_frozen_report_and_changes_require_a_new_review():
    from research_copilot.contracts import ArtifactRef, Dispatch, ReviewResult
    from research_copilot.runtime.business import BusinessCoordinator

    repository = ControlRepository()
    plan = ArtifactRef(artifact_id="plan", version="1", content_hash="b" * 64)
    draft = ArtifactRef(artifact_id="report", version="1", content_hash="a" * 64)
    repository.state.artifacts = {"plan": plan, "report": draft}
    repository.state.approved["plan"] = plan
    business = BusinessCoordinator(repository)
    dispatch = Dispatch("review-1", "reviewer", "/workspace/tasks/review-1/")
    await business.authorize_dispatch(context(), dispatch)
    result = ReviewResult(
        status="passed",
        summary="passed current version",
        reviewed=draft,
        issues=[],
        comments=[],
    )
    await business.accept_result(context(), dispatch, result)
    assert repository.state.reviewed_report == draft
    repository.state.artifacts["report"] = draft.model_copy(update={"version": "2"})
    with pytest.raises(ValueError, match="stale"):
        await business.accept_result(context(), dispatch, result)
