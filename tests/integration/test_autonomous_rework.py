"""Native rework changes real analysis/report versions without human middle gates."""

import pytest
from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.runtime.control import active_dispatch
from research_copilot.runtime.signoff import signoff_error
from research_copilot.subagents.analysist import AnalysisService, AnalysisCandidate
from research_copilot.subagents.writer import WritingService
from tests.integration.test_reviewer import submitted_draft
from tests.integration.test_writer import markdown
from tests.unit.test_orchestration import context


async def test_native_analysis_rework_requires_new_analysis_report_and_review(role_env):
    services, business, store = role_env
    await submitted_draft(role_env)

    def prepare(state):
        # Prepared immutable products, not a migration of a user session.
        state.role_data["workflow_version"] = "products-v2"
        state.approved.clear()
        state.role_data["reviews"] = {
            "changes": {
                "review_id": "changes",
                "outcome": "changes_requested",
                "reviewed": state.artifacts["report"].model_dump(),
                "issues": [{"issue_id": "i1", "severity": "major"}],
            }
        }

    await store.mutate_control(context(), prepare)
    await business.set_report_scope(
        context(),
        {
            "report_type": "company_report",
            "objective": "research",
            "as_of": "2025-04-30",
        },
    )
    before = await store.read_control(context())
    await business.propose_rework(
        context(),
        "changes",
        [{"role": "analysist", "objective": "Correct conclusion", "issue_ids": ["i1"]}],
    )

    async def dispatch(ident, role):
        binding = Dispatch(ident, role, f"/workspace/tasks/{ident}/")
        task = TaskBrief(
            task_id=ident,
            role=role,
            objective="Correct conclusion",
            calculation_mode="custom",  # Reuse registered metrics; no new arithmetic.
            as_of="2025-04-30",
            feedback_refs=["changes"],
        )
        await services.prepare_dispatch(context(), binding, task.model_dump_json())
        return binding

    with pytest.raises(ValueError, match="new analysis"):
        await dispatch("premature-writing", "writer")
    binding = await dispatch("native-analysis", "analysist")
    token = active_dispatch.set(binding)
    try:
        payload = dict(
            before.role_data["objects"][before.artifacts["analysis"].artifact_id][
                "payload"
            ]
        )
        payload.pop("verified_facts", None)
        payload["summary"] = "Corrected source-supported analysis"
        result = await AnalysisService(services).publish_structured(
            context(), AnalysisCandidate.model_validate(payload), "fix"
        )
    finally:
        active_dispatch.reset(token)
    with pytest.raises(ValueError, match="new report"):
        await dispatch("premature-review", "reviewer")
    binding = await dispatch("native-writing", "writer")
    token = active_dispatch.set(binding)
    try:
        path = services.report_root(context()) / "tasks/native-writing/draft.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown(services), encoding="utf-8")
        await WritingService(services).submit(
            context(),
            "rewrite",
            candidate_path="/workspace/tasks/native-writing/draft.md",
            report_type="company_report",
            analysis_ids=[result["analysis_id"]],
        )
    finally:
        active_dispatch.reset(token)
    await dispatch("native-review", "reviewer")
    state = await store.read_control(context())
    assert not state.approved
    assert state.artifacts["analysis"] != before.artifacts["analysis"]
    assert state.artifacts["report"] != before.artifacts["report"]
    assert (
        signoff_error(state, {"usage_verified": True})
        == "Current report has not passed review"
    )
    assert (
        "premature-writing" not in state.dispatches
        and "premature-review" not in state.dispatches
    )
