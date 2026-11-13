import pytest

from tests.unit.test_business import ControlRepository
from tests.unit.test_orchestration import context
from research_copilot.contracts import ArtifactRef, Dispatch
from research_copilot.runtime.business import (
    BusinessCoordinator,
    ResearchPlan,
    approval_key,
)


def _brief(task_id, role="searcher"):
    from research_copilot.contracts import TaskBrief

    return TaskBrief(task_id=task_id, role=role, objective=task_id, as_of="2025-04-30")


def setup_rework():
    repo = ControlRepository()
    for kind in ("plan", "analysis", "report"):
        repo.state.artifacts[kind] = ArtifactRef(
            artifact_id=kind, version="1", content_hash="a" * 64
        )
    repo.state.approved = {
        k: v for k, v in repo.state.artifacts.items() if k != "report"
    }
    repo.state.role_data["reviews"] = {
        "r": {
            "review_id": "r",
            "outcome": "changes_requested",
            "reviewed": repo.state.artifacts["report"].model_dump(),
            "issues": [{"issue_id": "i", "severity": "major"}],
            "feedback": {},
        }
    }
    return repo, BusinessCoordinator(repo)


@pytest.mark.asyncio
async def test_rework_preserves_visible_artifacts_and_invalidates_analysis_approval():
    repo, business = setup_rework()
    before = dict(repo.state.artifacts)
    tasks = [{"role": "analysist", "objective": "Correct formula", "issue_ids": ["i"]}]
    ref = await business.propose_rework(context(), "r", tasks)
    assert all(repo.state.artifacts[k] == v for k, v in before.items())
    assert "analysis" not in repo.state.approved
    assert repo.state.approved["plan"] == before["plan"]
    assert await business.propose_rework(context(), "r", tasks) == ref
    with pytest.raises(PermissionError):
        await business.authorize_dispatch(
            context(), Dispatch("d", "analysist", "/workspace/tasks/d/")
        )
    key = approval_key(context().report_id, "confirm_rework", ref)
    repo.state.grants[key] = context().actor_id
    await business.confirm(context(), "confirm_rework", ref, key)
    await business.authorize_dispatch(
        context(), Dispatch("d", "analysist", "/workspace/tasks/d/")
    )


@pytest.mark.asyncio
async def test_pending_review_cannot_be_replanned_or_approved_after_base_changes():
    repo, business = setup_rework()
    with pytest.raises(ValueError, match="use_propose_rework"):
        await business.propose_plan(
            context(),
            ResearchPlan(
                report_type="flash_comment",
                objective="rework",
                tasks=[_brief("fix", "writer")],
            ),
        )
    ref = await business.propose_rework(
        context(), "r", [{"role": "writer", "objective": "fix", "issue_ids": ["i"]}]
    )
    assert "analysis" in repo.state.approved
    repo.state.artifacts["report"] = repo.state.artifacts["report"].model_copy(
        update={"version": "2"}
    )
    key = approval_key(context().report_id, "confirm_rework", ref)
    repo.state.grants[key] = context().actor_id
    with pytest.raises(ValueError, match="base"):
        await business.confirm(context(), "confirm_rework", ref, key)


@pytest.mark.asyncio
async def test_rework_rejects_unknown_issues_and_changed_tasks_invalidate_confirmation():
    repo, business = setup_rework()
    with pytest.raises(ValueError, match="issue"):
        await business.propose_rework(
            context(),
            "r",
            [{"role": "writer", "objective": "fix", "issue_ids": ["fake"]}],
        )
    first = await business.propose_rework(
        context(), "r", [{"role": "writer", "objective": "fix", "issue_ids": ["i"]}]
    )
    repo.state.approved["rework"] = first
    second = await business.propose_rework(
        context(),
        "r",
        [{"role": "writer", "objective": "fix wording", "issue_ids": ["i"]}],
    )
    assert first != second
    assert "rework" not in repo.state.approved


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_explicit_recovery_uses_review_and_report_lineage_without_restoring_approval(
    tmp_path,
    native,
):
    from types import SimpleNamespace
    from research_copilot.subagents.services import digest, canonical

    repo, business = setup_rework()
    analysis_payload = {"summary": "original analysis"}
    analysis_ref = ArtifactRef(
        artifact_id="a", version="1", content_hash=digest(analysis_payload)
    )
    report_payload = {
        "document": "original report",
        "analysis_ref": analysis_ref.model_dump(),
    }
    report_ref = ArtifactRef(
        artifact_id="rpt", version="1", content_hash=digest(report_payload)
    )
    objects = {}
    for kind, ref, payload in [
        ("analysis", analysis_ref, analysis_payload),
        ("report", report_ref, report_payload),
    ]:
        path = tmp_path / (ref.artifact_id + ".json")
        path.write_text(canonical(payload), encoding="utf-8")
        objects[ref.artifact_id] = {
            "kind": kind,
            "ref": ref.model_dump(),
            "payload": payload,
            "path": "/workspace/" + path.name,
            "dispatch_id": "old",
        }
    repo.state.role_data["objects"] = objects
    import hashlib

    original = ResearchPlan(
        report_type="flash_comment", objective="original", tasks=[_brief("collect")]
    )
    plan_ref = ArtifactRef(
        artifact_id="plan",
        version="1",
        content_hash=hashlib.sha256(original.model_dump_json().encode()).hexdigest(),
    )
    repo.state.plans = [
        {"artifact": plan_ref.model_dump(), "plan": original.model_dump()}
    ]
    repo.state.dispatches["old"] = {
        "role": "writer",
        "inputs": {"plan": plan_ref.model_dump()},
    }
    repo.state.role_data["reviews"]["r"]["reviewed"] = report_ref.model_dump()
    repo.state.artifacts = {"plan": repo.state.artifacts["plan"]}
    if native:
        from research_copilot.runtime.autonomy import freeze_scope

        repo.state.role_data["workflow_version"] = "products-v2"
        ref = freeze_scope(
            repo.state,
            context().report_id,
            {
                "report_type": "company_report",
                "objective": "research",
                "as_of": "2025-04-30",
            },
        )
        repo.state.dispatches["old"]["inputs"] = {"scope": ref.model_dump()}
    business.role_services = SimpleNamespace(report_root=lambda ctx: tmp_path)
    await business.recover_rework(context(), "r")
    assert repo.state.artifacts["report"] == report_ref
    assert repo.state.artifacts["analysis"] == analysis_ref
    assert "report" not in repo.state.approved
    (tmp_path / "rpt.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        await business.recover_rework(context(), "r")


@pytest.mark.asyncio
async def test_revoked_receipt_needs_fresh_human_grant():
    repo, business = setup_rework()
    ref = repo.state.artifacts["analysis"]
    key = approval_key(context().report_id, "confirm_analysis", ref)
    repo.state.approved.pop("analysis")
    repo.state.receipts[key] = {"receipt_id": "old"}
    with pytest.raises(PermissionError):
        await business.confirm(context(), "confirm_analysis", ref, key)
    repo.state.grants[key] = context().actor_id
    await business.confirm(context(), "confirm_analysis", ref, key)
    assert repo.state.approved["analysis"] == ref


@pytest.mark.asyncio
async def test_analysis_rework_cannot_reconfirm_base_analysis_or_use_old_dispatch():
    from research_copilot.runtime.rework import validate_dispatch

    repo, business = setup_rework()
    base = repo.state.artifacts["analysis"]
    old = {
        "role": "analysist",
        "inputs": {k: v.model_dump() for k, v in repo.state.artifacts.items()},
    }
    rework = await business.propose_rework(
        context(), "r", [{"role": "analysist", "objective": "fix", "issue_ids": ["i"]}]
    )
    repo.state.approved["rework"] = rework
    with pytest.raises(PermissionError, match="version"):
        validate_dispatch(repo.state, old)
    key = approval_key(context().report_id, "confirm_analysis", base)
    repo.state.grants[key] = context().actor_id
    with pytest.raises(PermissionError, match="newly"):
        await business.confirm(context(), "confirm_analysis", base, key)
