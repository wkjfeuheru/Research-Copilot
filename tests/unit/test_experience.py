import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef, RunContext
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.experience import (
    EXPERIENCE_ROLES,
    PROBLEM_TYPE_TAGS,
    STAGE_BY_ROLE,
    ExperienceDraft,
    ExperienceExtractionError,
    ExperienceService,
    ExtractionResult,
    RetrospectiveWorker,
    build_retrospective_materials,
    classify_problem_type,
    experiences,
    normalize_drafts,
)
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from tests.unit.test_orchestration import context


async def _store(tmp_path, report_id="report-1", actor_id="actor-1", budget_id=None):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / (report_id + '.db')}"
    )
    store = SqlSessionStore(engine)
    await store.setup()
    budget_id = budget_id or ("budget-1" if report_id == "report-1" else report_id)
    await store.create(
        SessionRecord(
            report_id,
            actor_id,
            "v2",
            "policy-1",
            budget_id,
            run_id="run-1",
        ),
        TokenBudget(limit=100000, budget_id=budget_id).snapshot(),
    )
    return store, engine


def _experience(
    *,
    role="writer",
    stage="writing",
    problem_type="numeric_accuracy",
    created_at=None,
    enabled=True,
    source_report_id="source-report",
    source_version="1",
):
    return {
        "id": uuid4().hex,
        "source_report_id": source_report_id,
        "source_report_version": source_version,
        "source_job_id": "job-1",
        "role": role,
        "stage": stage,
        "problem_type": problem_type,
        "applies_when": "审阅退回指出数字单位或期间不一致时。",
        "recommendation": "先冻结口径与单位，再逐项对照原证据。",
        "evidence_refs": [f"review:{source_report_id}:issue-1"],
        "enabled": enabled,
        "disabled_at": None,
        "disabled_by": None,
        "fingerprint": hashlib.sha256(uuid4().hex.encode()).hexdigest(),
        "created_at": created_at or datetime.now(UTC),
    }


async def _insert(engine, rows):
    async with engine.begin() as connection:
        await connection.execute(experiences.insert(), rows)


def _delivered_state(report_ref, analysis_ref, plan_ref, *, with_basis=True):
    from research_copilot.runtime.business import ControlState

    state = ControlState()
    state.artifacts["plan"] = plan_ref
    state.approved["plan"] = plan_ref
    state.artifacts["analysis"] = analysis_ref
    state.approved["analysis"] = analysis_ref
    state.artifacts["report"] = report_ref
    state.reviewed_report = report_ref
    state.role_data["objects"] = {
        report_ref.artifact_id: {
            "kind": "report",
            "ref": report_ref.model_dump(),
            "payload": {"analysis_ref": analysis_ref.model_dump()},
        }
    }
    if with_basis:
        state.role_data["reviews"] = {
            "review-1": {
                "review_id": "review-1",
                "reviewed": report_ref.model_dump(),
                "outcome": "changes_requested",
                "checks_complete": True,
                "pass_confidence": 0.8,
                "summary": "需要修正数字口径",
                "feedback": {"comment": "请核对单位"},
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
                        "suggestion": "统一为百分点",
                    }
                ],
                "issue_closures": [],
            }
        }
        state.role_data["rework"] = {
            "rework-1": {
                "review_id": "review-1",
                "tasks": [
                    {
                        "role": "writer",
                        "objective": "修正第2章单位",
                        "issue_ids": ["issue-1"],
                    }
                ],
                "base_report_ref": report_ref.model_dump(),
                "base_analysis_ref": analysis_ref.model_dump(),
                "base_plan_ref": plan_ref.model_dump(),
            }
        }
    return state


def _refs():
    plan = ArtifactRef(artifact_id="plan:report-1", version="1", content_hash="a" * 64)
    analysis = ArtifactRef(artifact_id="analysis-1", version="1", content_hash="b" * 64)
    report = ArtifactRef(artifact_id="report-1", version="1", content_hash="c" * 64)
    return plan, analysis, report


@pytest.mark.asyncio
async def test_recall_filters_role_stage_disabled_and_ranks_problem_type_then_recency(
    tmp_path,
):
    store, engine = await _store(tmp_path)
    try:
        now = datetime.now(UTC)
        rows = [
            _experience(
                problem_type="numeric_accuracy", created_at=now - timedelta(days=4)
            ),
            _experience(
                problem_type="numeric_accuracy", created_at=now - timedelta(days=1)
            ),
            _experience(
                problem_type="source_quality", created_at=now - timedelta(days=2)
            ),
            _experience(
                problem_type="numeric_accuracy",
                created_at=now - timedelta(hours=1),
                enabled=False,
            ),
            _experience(role="searcher", stage="collect", created_at=now),
        ]
        await _insert(engine, rows)
        recalled = await store.experience.recall(
            role="writer", stage="writing", problem_type="numeric_accuracy"
        )
        assert [item["problem_type"] for item in recalled] == [
            "numeric_accuracy",
            "numeric_accuracy",
            "source_quality",
        ]
        assert all(item["enabled"] is True for item in recalled)
        assert len(recalled) == 3
        plain = await store.experience.recall(role="writer", stage="writing")
        assert [item["problem_type"] for item in plain] == [
            "numeric_accuracy",
            "source_quality",
            "numeric_accuracy",
        ]
        empty = await store.experience.recall(role="analysist", stage="analysis")
        assert empty == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_signoff_registers_one_job_only_for_delivered_report(tmp_path):
    from research_copilot.runtime.business import BusinessCoordinator, approval_key

    store, engine = await _store(tmp_path)
    try:
        plan, analysis, report = _refs()
        before = await store.experience.jobs("report-1")
        assert before == []

        def seed(state):
            seeded = _delivered_state(report, analysis, plan, with_basis=False)
            state.artifacts = seeded.artifacts
            state.approved = seeded.approved
            state.reviewed_report = seeded.reviewed_report
            state.role_data = seeded.role_data
            state.role_data["reviews"] = {
                "passed": {
                    "reviewed": report.model_dump(),
                    "outcome": "passed",
                    "checks_complete": True,
                    "pass_confidence": 0.99,
                    "issues": [],
                }
            }
            state.grants[approval_key("report-1", "signoff_report", report)] = "actor-1"

        await store.mutate_control(context(), seed)
        business = BusinessCoordinator(store)
        key = approval_key("report-1", "signoff_report", report)
        first = await business.confirm(context(), "signoff_report", report, key)
        second = await business.confirm(context(), "signoff_report", report, key)
        assert first == second
        jobs = await store.experience.jobs("report-1")
        assert len(jobs) == 1
        assert jobs[0]["status"] == "pending"
        assert jobs[0]["report_version"] == "1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_materials_include_review_rework_and_only_known_refs():
    plan, analysis, report = _refs()
    state = _delivered_state(report, analysis, plan)
    materials = build_retrospective_materials(state, report)
    assert materials["has_basis"] is True
    assert "review:review-1" in materials["allowed_evidence_refs"]
    assert "review:review-1/issue:issue-1" in materials["allowed_evidence_refs"]
    assert "rework:rework-1" in materials["allowed_evidence_refs"]
    assert materials["reviews"][0]["issues"][0]["category"] == "数字与单位"

    valid = ExperienceDraft(
        role="writer",
        stage="writing",
        problem_type="numeric_accuracy",
        applies_when="审阅退回指出数字单位或期间不一致时。",
        recommendation="先冻结口径与单位，再逐项对照原证据。",
        evidence_refs=["review:review-1/issue:issue-1"],
    )
    assert normalize_drafts([valid], set(materials["allowed_evidence_refs"]))
    with pytest.raises(ExperienceExtractionError):
        normalize_drafts(
            [valid.model_copy(update={"evidence_refs": ["review:nope"]})],
            set(materials["allowed_evidence_refs"]),
        )
    with pytest.raises(ValueError):
        ExperienceDraft(
            role="writer",
            stage="writing",
            problem_type="not-a-tag",
            applies_when="审阅退回指出数字单位或期间不一致时。",
            recommendation="先冻结口径与单位，再逐项对照原证据。",
            evidence_refs=["review:review-1/issue:issue-1"],
        )


class _Extractor:
    def __init__(self, draft, usage=None):
        self.draft = draft
        self.usage = usage or {"input_tokens": 10, "output_tokens": 5}
        self.calls = 0

    async def extract(self, materials):
        self.calls += 1
        return ExtractionResult(
            drafts=(
                self.draft.model_copy(
                    update={"evidence_refs": [materials["allowed_evidence_refs"][1]]}
                ),
            ),
            usage=self.usage,
        )


@pytest.mark.asyncio
async def test_worker_completes_once_and_recovers_stale_claim(tmp_path):
    from research_copilot.runtime.business import approval_key
    from research_copilot.runtime.experience import enqueue_delivery

    store, engine = await _store(tmp_path)
    try:
        plan, analysis, report = _refs()
        state = _delivered_state(report, analysis, plan)
        state.approved["report"] = report
        state.grants[approval_key("report-1", "signoff_report", report)] = "actor-1"

        def seed(s):
            s.artifacts = state.artifacts
            s.approved = state.approved
            s.reviewed_report = state.reviewed_report
            s.role_data = state.role_data
            s.grants = state.grants

        await store.mutate_control(context(), seed)
        # The same transaction shape used by signoff.
        async with engine.begin() as connection:
            await enqueue_delivery(
                connection,
                report_id="report-1",
                report_version="1",
                report_artifact_id=report.artifact_id,
                report_content_hash=report.content_hash,
                actor_id="actor-1",
            )
        claim = await store.experience.claim(lease_seconds=1)
        assert claim is not None
        # Only expiration of an unstarted claim permits another worker to take it.
        from research_copilot.runtime.experience import retrospective_jobs

        assert await store.experience.claim() is None
        async with engine.begin() as connection:
            await connection.execute(
                retrospective_jobs.update()
                .where(retrospective_jobs.c.id == claim["id"])
                .values(locked_until=datetime(2000, 1, 1, tzinfo=UTC))
            )
        draft = ExperienceDraft(
            role="writer",
            stage="writing",
            problem_type="numeric_accuracy",
            applies_when="审阅退回指出数字单位或期间不一致时。",
            recommendation="先冻结口径与单位，再逐项对照原证据。",
            evidence_refs=["review:review-1/issue:issue-1"],
        )
        extractor = _Extractor(draft)
        worker = RetrospectiveWorker(store, extractor, lease_seconds=1)
        assert await worker.run_once() is True
        jobs = await store.experience.jobs("report-1")
        assert jobs[0]["status"] == "completed", jobs[0]["last_error"]
        assert jobs[0]["experience_count"] == 1
        assert jobs[0]["usage"]["input_tokens"] == 10
        view = await store.experience.report_view("report-1")
        assert len(view["produced"]) == 1
        assert view["produced"][0]["source_report_id"] == "report-1"
        assert await worker.run_once() is False
        assert extractor.calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_records_unknown_failure_without_retry(tmp_path):
    from research_copilot.runtime.experience import enqueue_delivery

    store, engine = await _store(tmp_path)
    try:
        plan, analysis, report = _refs()
        state = _delivered_state(report, analysis, plan)
        state.approved["report"] = report

        def seed(s):
            s.artifacts = state.artifacts
            s.approved = state.approved
            s.reviewed_report = state.reviewed_report
            s.role_data = state.role_data

        await store.mutate_control(context(), seed)
        async with engine.begin() as connection:
            await enqueue_delivery(
                connection,
                report_id="report-1",
                report_version="1",
                report_artifact_id=report.artifact_id,
                report_content_hash=report.content_hash,
                actor_id="actor-1",
            )

        class Broken:
            async def extract(self, materials):
                raise RuntimeError("model unavailable")

        worker = RetrospectiveWorker(store, Broken(), lease_seconds=1)
        assert await worker.run_once() is True
        job = (await store.experience.jobs("report-1"))[0]
        assert job["status"] == "failed", job["last_error"]
        assert job["attempts"] == 1
        assert "model unavailable" in job["last_error"]
        assert "recovery_required" in job["last_error"]
        # Even after the retry window, unknown usage must not trigger another call.
        from research_copilot.runtime.experience import retrospective_jobs

        async with engine.begin() as connection:
            await connection.execute(
                retrospective_jobs.update()
                .where(retrospective_jobs.c.id == job["id"])
                .values(
                    available_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
        assert await worker.run_once() is False
        job = (await store.experience.jobs("report-1"))[0]
        assert job["status"] == "failed"
        assert job["attempts"] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recall_and_adoption_effects_are_idempotent(tmp_path):

    store, engine = await _store(tmp_path)
    try:
        await _insert(engine, [_experience()])
        experience_id = (await store.experience.recall(role="writer", stage="writing"))[
            0
        ]["experience_id"]

        def noop(state):
            return "ok"

        recall_effect = {
            "kind": "experience_recall",
            "report_id": "report-1",
            "dispatch_id": "dispatch-1",
            "role": "writer",
            "stage": "writing",
            "experience_ids": [experience_id],
        }
        await store.mutate_control(context(), noop, effects=[recall_effect])
        await store.mutate_control(context(), noop, effects=[recall_effect])
        view = await store.experience.report_view("report-1")
        assert len(view["received"]) == 1
        assert view["received"][0]["usage"]["adopted"] is False

        adopt = {
            "kind": "experience_adopted",
            "report_id": "report-1",
            "dispatch_id": "dispatch-1",
            "experience_ids": [experience_id],
        }
        await store.mutate_control(context(), noop, effects=[adopt])
        view = await store.experience.report_view("report-1")
        assert view["received"][0]["usage"]["adopted"] is True

        with pytest.raises(Exception):
            await store.mutate_control(
                context(),
                noop,
                effects=[{**adopt, "experience_ids": ["unknown"]}],
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_report_view_and_disable_are_actor_scoped(tmp_path):
    store, engine = await _store(
        tmp_path, report_id="source-report", actor_id="actor-1"
    )
    try:
        # The consumer report belongs to the same local actor in this single-user host.
        await store.create(
            SessionRecord(
                "consumer-report",
                "actor-1",
                "v2",
                "policy-1",
                "consumer-report",
                run_id="run-2",
            ),
            TokenBudget(limit=100000, budget_id="consumer-report").snapshot(),
        )
        await _insert(engine, [_experience(source_report_id="source-report")])
        source = (await store.experience.recall(role="writer", stage="writing"))[0]
        service = ExperienceService(store)

        def noop(state):
            return None

        consumer_context = RunContext(
            "actor-1", "consumer-report", "run-2", "consumer-report", "policy-1"
        )
        await store.mutate_control(
            consumer_context,
            noop,
            effects=[
                {
                    "kind": "experience_recall",
                    "report_id": "consumer-report",
                    "dispatch_id": "dispatch-1",
                    "role": "writer",
                    "stage": "writing",
                    "experience_ids": [source["experience_id"]],
                }
            ],
        )
        view = await service.report_view("actor-1", "consumer-report")
        assert len(view["received"]) == 1
        assert view["received"][0]["source_report_id"] == "source-report"
        assert len(await service.recall("writer", "writing")) == 1

        updated = await service.set_enabled("actor-1", source["experience_id"], False)
        assert updated["enabled"] is False
        assert await service.recall("writer", "writing") == []
        with pytest.raises(PermissionError):
            await service.set_enabled("other-actor", source["experience_id"], True)
    finally:
        await engine.dispose()


def test_problem_type_vocabulary_and_mapping():
    assert classify_problem_type("数字、单位与期间") == "numeric_accuracy"
    assert classify_problem_type("DCF 假设与敏感性") == "valuation_method"
    assert classify_problem_type("数据来源不足") == "source_quality"
    assert classify_problem_type("完全无关") is None
    assert set(STAGE_BY_ROLE) == set(EXPERIENCE_ROLES)
    assert "numeric_accuracy" in PROBLEM_TYPE_TAGS


@pytest.mark.asyncio
async def test_dispatch_recall_writes_snapshot_and_marks_only_explicit_adoption(
    tmp_path,
):
    from research_copilot.contracts import TaskBrief
    from research_copilot.runtime.business import BusinessCoordinator
    from research_copilot.subagents.contracts import BusinessInputError, SearchResult
    from research_copilot.subagents.services import RoleServices

    store, engine = await _store(tmp_path)
    try:
        await _insert(
            engine,
            [
                _experience(
                    role="searcher",
                    stage="collect",
                    problem_type="source_quality",
                    source_report_id="source-report",
                )
            ],
        )
        experience_id = (
            await store.experience.recall(role="searcher", stage="collect")
        )[0]["experience_id"]

        business = BusinessCoordinator(store)
        from research_copilot.runtime.autonomy import ReportScope

        await business.set_report_scope(
            context(),
            ReportScope(
                report_type="company_report", objective="research", as_of="2025-04-30"
            ),
        )
        services = RoleServices(
            repository=store,
            workspace_root=tmp_path / "workspace",
            business=business,
            report_rules={"version": 1},
            experience=ExperienceService(store),
        )
        from research_copilot.contracts import Dispatch

        dispatch = Dispatch("dispatch-1", "searcher", "/workspace/tasks/dispatch-1/")
        await business.authorize_dispatch(context(), dispatch)
        brief = TaskBrief(
            task_id="collect",
            role="searcher",
            objective="collect facts",
            as_of="2025-04-30",
        )
        info = await services.prepare_dispatch(
            context(), dispatch, json.dumps(brief.model_dump(), ensure_ascii=False)
        )
        assert info["experience_ids"] == [experience_id]
        assert info["experience_path"] == (
            "/workspace/artifacts/dispatch-1/experiences.json"
        )
        file = (
            tmp_path
            / "workspace"
            / "report-1"
            / "artifacts"
            / "dispatch-1"
            / "experiences.json"
        )
        assert file.is_file()
        assert experience_id in file.read_text(encoding="utf-8")
        view = await store.experience.report_view("report-1")
        assert view["received"][0]["usage"]["adopted"] is False

        result = SearchResult(
            task_id="collect",
            status="completed",
            summary="done",
            used_experience_ids=[experience_id],
        )
        mapped = await services.map_result(context(), dispatch, result.model_dump())
        assert mapped.used_experience_ids == [experience_id]
        view = await store.experience.report_view("report-1")
        assert view["received"][0]["usage"]["adopted"] is True

        with pytest.raises(BusinessInputError):
            await services.map_result(
                context(),
                dispatch,
                result.model_copy(
                    update={"used_experience_ids": ["unknown"]}
                ).model_dump(),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_jobs_are_one_per_delivery_version_and_not_created_for_cancelled(
    tmp_path,
):
    from research_copilot.runtime.experience import enqueue_delivery

    store, engine = await _store(tmp_path)
    try:
        async with engine.begin() as connection:
            for version in ("1", "1", "2"):
                await enqueue_delivery(
                    connection,
                    report_id="report-1",
                    report_version=version,
                    report_artifact_id="report-" + version,
                    report_content_hash="c" * 64,
                    actor_id="actor-1",
                )
        jobs = await store.experience.jobs("report-1")
        assert [job["report_version"] for job in jobs] == ["1", "2"]
        await store.request_cancel("report-1", "actor-1")
        assert len(await store.experience.jobs("report-1")) == 2
        assert await store.experience.jobs("never-delivered") == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_llm_extractor_parses_json_fence_and_records_model_usage():
    from langchain_core.messages import AIMessage

    from research_copilot.runtime.experience import (
        ExperienceExtractionError,
        LLMExperienceExtractor,
    )

    class FakeModel:
        model_name = "fake-experience-model"
        profile = {"max_output_tokens": 512}

        def __init__(self, content):
            self.content = content
            self.calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            return AIMessage(
                content=self.content,
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "total_tokens": 19,
                },
            )

    payload = {
        "experiences": [
            {
                "role": "writer",
                "stage": "writing",
                "problem_type": "numeric_accuracy",
                "applies_when": "审阅退回指出数字单位或期间不一致时。",
                "recommendation": "先冻结口径与单位，再按原证据逐项重算。",
                "evidence_refs": ["review:r/issue:i"],
            }
        ]
    }
    model = FakeModel("```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")
    result = await LLMExperienceExtractor(model).extract({"allowed_evidence_refs": []})
    assert len(result.drafts) == 1
    assert result.usage["model"] == "fake-experience-model"
    assert result.usage["input_tokens"] == 12
    assert model.calls == 1

    with pytest.raises(ExperienceExtractionError):
        await LLMExperienceExtractor(FakeModel("not json")).extract({})


@pytest.mark.asyncio
async def test_failed_or_cancelled_delivery_never_enqueues_a_job(tmp_path):
    from research_copilot.runtime.business import BusinessCoordinator, approval_key

    store, engine = await _store(tmp_path)
    try:
        plan, analysis, report = _refs()

        def seed(state):
            state.artifacts["plan"] = plan
            state.approved["plan"] = plan
            state.artifacts["analysis"] = analysis
            state.approved["analysis"] = analysis
            state.artifacts["report"] = report
            # No reviewed_report: signoff must fail before the effect is appended.
            state.grants[approval_key("report-1", "signoff_report", report)] = "actor-1"

        await store.mutate_control(context(), seed)
        business = BusinessCoordinator(store)
        with pytest.raises(PermissionError):
            await business.confirm(
                context(),
                "signoff_report",
                report,
                approval_key("report-1", "signoff_report", report),
            )
        assert await store.experience.jobs("report-1") == []
        await store.request_cancel("report-1", "actor-1")
        assert await store.experience.jobs("report-1") == []
    finally:
        await engine.dispose()


def test_retrospective_materials_keep_prior_rejection_and_correction_for_v2():

    plan, analysis, report_v1 = _refs()
    report_v2 = ArtifactRef(artifact_id="report-1", version="2", content_hash="d" * 64)
    state = _delivered_state(report_v1, analysis, plan)
    state.artifacts["report"] = report_v2
    state.reviewed_report = report_v2
    state.role_data["objects"][report_v2.artifact_id] = {
        "kind": "report",
        "ref": report_v2.model_dump(),
        "payload": {"analysis_ref": analysis.model_dump()},
    }
    state.role_data["reviews"]["review-2"] = {
        "review_id": "review-2",
        "reviewed": report_v2.model_dump(),
        "outcome": "passed",
        "checks_complete": True,
        "pass_confidence": 0.95,
        "summary": "修正后通过",
        "feedback": {"comment": ""},
        "issues": [],
        "issue_closures": [
            {
                "issue_id": "issue-1",
                "target_id": "document:0",
                "quote": "收入增长 5 个百分点",
                "reason": "已改为百分点",
            }
        ],
    }
    materials = build_retrospective_materials(state, report_v2)
    assert materials["has_basis"] is True
    assert {review["review_id"] for review in materials["reviews"]} == {
        "review-1",
        "review-2",
    }
    assert materials["rework"][0]["base_report_version"] == "1"
    assert "review:review-1/issue:issue-1" in materials["allowed_evidence_refs"]
    assert "review:review-2/closure:issue-1" in materials["allowed_evidence_refs"]
