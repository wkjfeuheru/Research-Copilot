"""Offline A -> B acceptance: signoff, retrospective, recall, traceability, disable."""

import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef, Dispatch, RunContext, TaskBrief
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.business import (
    BusinessCoordinator,
    ResearchPlan,
    approval_key,
)
from research_copilot.runtime.experience import (
    ExperienceDraft,
    ExperienceService,
    ExtractionResult,
    RetrospectiveWorker,
)
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.subagents.services import RoleServices


class _Extractor:
    def __init__(self, ref):
        self.ref = ref

    async def extract(self, materials):
        return ExtractionResult(
            drafts=(
                ExperienceDraft(
                    role="writer",
                    stage="writing",
                    problem_type="numeric_accuracy",
                    applies_when="审阅退回指出数字单位或期间不一致时。",
                    recommendation="先冻结口径与单位，再按原证据逐项重算并复述差异。",
                    evidence_refs=[self.ref],
                ),
            ),
            usage={"input_tokens": 20, "output_tokens": 10},
        )


async def _create(store, report_id):
    await store.create(
        SessionRecord(
            report_id,
            "actor-1",
            "v2",
            "policy-1",
            report_id,
            run_id="run-" + report_id,
            status="idle",
        ),
        TokenBudget(limit=100000, budget_id=report_id).snapshot(),
        workflow_version=None,  # Legacy business-contract fixture; no execution entrypoint.
    )


async def _seed_delivered(store, report, analysis, plan):
    def seed(state):
        state.artifacts["plan"] = plan
        state.approved["plan"] = plan
        state.artifacts["analysis"] = analysis
        state.approved["analysis"] = analysis
        state.artifacts["report"] = report
        state.reviewed_report = report
        state.role_data["objects"] = {
            report.artifact_id: {
                "kind": "report",
                "ref": report.model_dump(),
                "payload": {"analysis_ref": analysis.model_dump()},
            }
        }
        state.role_data["reviews"] = {
            "review-1": {
                "review_id": "review-1",
                "reviewed": report.model_dump(),
                "outcome": "changes_requested",
                "checks_complete": True,
                "pass_confidence": 0.8,
                "summary": "数字口径需要修正",
                "feedback": {"comment": "请统一单位"},
                "issues": [
                    {
                        "issue_id": "issue-1",
                        "category": "数字与单位",
                        "severity": "major",
                        "target_id": "document:0",
                        "location": "第2章",
                        "quote": "收入增长 5%",
                        "basis": "审阅发现单位不一致",
                        "reason": "百分比与百分点混用",
                        "suggestion": "统一口径",
                    }
                ],
            }
        }
        state.role_data["rework"] = {
            "rework-1": {
                "review_id": "review-1",
                "tasks": [
                    {
                        "role": "writer",
                        "objective": "修正数字单位",
                        "issue_ids": ["issue-1"],
                    }
                ],
                "base_report_ref": report.model_dump(),
                "base_analysis_ref": analysis.model_dump(),
                "base_plan_ref": plan.model_dump(),
            }
        }
        state.role_data["reviews"]["review-passed"] = {
            "review_id": "review-passed",
            "reviewed": report.model_dump(),
            "outcome": "passed",
            "checks_complete": True,
            "pass_confidence": 0.99,
            "issues": [],
        }
        state.grants[approval_key("report-a", "signoff_report", report)] = "actor-1"

    from research_copilot.contracts import RunContext

    await store.mutate_control(
        RunContext("actor-1", "report-a", "run-report-a", "report-a", "policy-1"),
        seed,
    )


@pytest.mark.asyncio
async def test_report_a_experience_is_recalled_by_report_b_and_stops_after_disable(
    tmp_path,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'flow.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    try:
        await _create(store, "report-a")
        await _create(store, "report-b")
        plan_ref = ArtifactRef(
            artifact_id="plan:report-a", version="1", content_hash="a" * 64
        )
        analysis_ref = ArtifactRef(
            artifact_id="analysis-1", version="1", content_hash="b" * 64
        )
        report_ref = ArtifactRef(
            artifact_id="report-1", version="1", content_hash="c" * 64
        )
        await _seed_delivered(store, report_ref, analysis_ref, plan_ref)
        business = BusinessCoordinator(store)
        context_a = RunContext(
            "actor-1", "report-a", "run-report-a", "report-a", "policy-1"
        )
        await business.confirm(
            context_a,
            "signoff_report",
            report_ref,
            approval_key("report-a", "signoff_report", report_ref),
        )
        jobs = await store.experience.jobs("report-a")
        assert len(jobs) == 1 and jobs[0]["status"] == "pending"

        worker = RetrospectiveWorker(
            store,
            _Extractor("review:review-1/issue:issue-1"),
            lease_seconds=30,
        )
        assert await worker.run_once() is True
        produced = (await store.experience.report_view("report-a"))["produced"]
        assert len(produced) == 1
        experience = produced[0]
        assert experience["source_report_id"] == "report-a"
        assert experience["source_report_version"] == "1"
        assert "review:review-1/issue:issue-1" in experience["evidence_refs"]

        # Report B, same writer stage, different report.
        context_b = RunContext(
            "actor-1", "report-b", "run-report-b", "report-b", "policy-1"
        )
        plan_b = await business.propose_plan(
            context_b,
            ResearchPlan(
                report_type="company_report",
                objective="另一标的",
                tasks=[
                    TaskBrief(
                        task_id="write",
                        role="writer",
                        objective="写作",
                        as_of="2025-04-30",
                    )
                ],
            ),
        )

        def approve_b(state):
            state.approved["plan"] = plan_b
            state.artifacts["analysis"] = analysis_ref
            state.approved["analysis"] = analysis_ref

        await store.mutate_control(context_b, approve_b)
        services = RoleServices(
            repository=store,
            workspace_root=tmp_path / "workspace",
            business=business,
            report_rules={"version": 1},
            experience=ExperienceService(store),
        )
        brief = TaskBrief(
            task_id="write", role="writer", objective="写作", as_of="2025-04-30"
        )
        first = Dispatch("dispatch-b1", "writer", "/workspace/tasks/dispatch-b1/")
        await business.authorize_dispatch(context_b, first)
        info = await services.prepare_dispatch(
            context_b, first, json.dumps(brief.model_dump(), ensure_ascii=False)
        )
        assert info["experience_ids"] == [experience["experience_id"]]
        view = await store.experience.report_view("report-b")
        assert view["received"][0]["experience_id"] == experience["experience_id"]
        assert view["received"][0]["source_report_id"] == "report-a"
        assert view["received"][0]["usage"]["adopted"] is False

        await ExperienceService(store).set_enabled(
            "actor-1", experience["experience_id"], False
        )
        second = Dispatch("dispatch-b2", "writer", "/workspace/tasks/dispatch-b2/")
        await business.authorize_dispatch(context_b, second)
        info = await services.prepare_dispatch(
            context_b, second, json.dumps(brief.model_dump(), ensure_ascii=False)
        )
        assert info["experience_ids"] == []
    finally:
        await engine.dispose()
