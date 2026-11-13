from pathlib import Path
from types import SimpleNamespace
import pytest
from tests.unit.test_rework import setup_rework


@pytest.mark.asyncio
async def test_driver_never_invents_human_signoff(tmp_path):
    from scripts.e2e_chain import build_decisions
    from research_copilot.runtime.budget import TokenBudget

    repo, _ = setup_rework()
    ref = repo.state.artifacts["report"]
    repo.state.reviewed_report = ref
    status = SimpleNamespace(
        interrupts=[
            {
                "id": "gate",
                "value": {
                    "action_requests": [
                        {
                            "name": "signoff_report",
                            "args": {"artifact": ref.model_dump()},
                        }
                    ]
                },
            }
        ]
    )
    result = await build_decisions(
        status, repo.state, TokenBudget(limit=1000, budget_id="b"), tmp_path, []
    )
    assert result is None
    assert (tmp_path / "pending-human-decisions.json").is_file()


def test_r5_manifest_freezes_new_id_and_stage_limits():
    from scripts.e2e_chain import build_manifest
    from research_copilot.runtime.host_settings import HostSettings

    manifest = build_manifest(
        HostSettings(_env_file=None), 2100000, report_id="r5-test"
    )
    assert manifest["report_id"] == "r5-test"
    # manifest 的模型调用上限冻结自 execution_policy.toml，随配置校准变化。
    from research_copilot.runtime.preflight import load_execution_policy

    assert (
        manifest["limits"]["model"]
        == load_execution_policy(Path("config/execution_policy.toml"))["call_limit"]
    )
    assert manifest["limits"]["source"] == 1
    assert manifest["stage_limits"]["reviewer"]["branches"] == {
        "quality": 16,
        "compliance": 6,
    }


@pytest.mark.asyncio
async def test_acceptance_never_passes_unknown_usage_or_unsigned_report():
    from research_copilot.runtime.acceptance import evaluate_acceptance
    from research_copilot.runtime.budget import TokenBudget

    repo, _ = setup_rework()
    budget = TokenBudget(limit=2100000, budget_id="r5")
    request = await budget.reserve(100)
    await budget.settle(request, None)
    result = evaluate_acceptance(repo.state, budget)
    assert not result["passed"]
    assert "usage_unknown" in result["failures"]
    assert "human_signoff_missing" in result["failures"]


def test_acceptance_uses_analysis_fact_scope_and_requires_prior_issue_closure():
    import hashlib
    from research_copilot.runtime.acceptance import evaluate_acceptance
    from research_copilot.runtime.budget import TokenBudget

    repo, _ = setup_rework()
    state = repo.state
    report, analysis, plan = [
        state.artifacts[k] for k in ("report", "analysis", "plan")
    ]
    body = "已验证的定性结论。"
    state.role_data.update(
        research_as_of="2025-04-30",
        regression_criteria={
            "required_fields": [f"k{i}" for i in range(11)],
            "optional_fields": [f"k{i}" for i in range(11, 14)],
        },
        regression_issues=[{"issue_id": "old-major", "severity": "major"}],
        objects={
            report.artifact_id: {
                "payload": {
                    "document": body,
                    "analysis_ref": analysis.model_dump(),
                    "rules": {"is_example": False},
                    "evidence_ids": ["E"],
                    "claims": {
                        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
                        "claims": [],
                    },
                }
            },
            analysis.artifact_id: {
                "dispatch_id": "a",
                "payload": {"metric_ids": [], "evidence_ids": ["E"], "claims": []},
            },
        },
        evidence={"E": {"kind": "attachment_original", "raw": {}}},
        scopes={
            "a": {
                "fact_sheet_ids": ["current"],
                "task_brief": {
                    "required_fields": [f"k{i}" for i in range(11)],
                    "optional_fields": [f"k{i}" for i in range(11, 14)],
                },
            }
        },
        fact_sheets={
            "current": {
                "plan": plan.model_dump(),
                "facts": [
                    {"input_ref": {"key": f"k{i}", "evidence_id": "E"}}
                    for i in range(14)
                ],
            }
        },
        reviews={
            "new": {
                "reviewed": report.model_dump(),
                "outcome": "passed",
                "checks_complete": True,
                "pass_confidence": 0.9,
                "issues": [],
                "issue_closures": [
                    {
                        "issue_id": "old-major",
                        "quote": body,
                        "reason": "reviewed corrected current report",
                    }
                ],
            }
        },
    )
    state.dispatches["a"] = {"inputs": {"plan": plan.model_dump()}}
    state.approved["report"] = report
    budget = TokenBudget(limit=2100000, budget_id="r5")
    assert evaluate_acceptance(state, budget)["passed"]
    state.role_data["fact_sheets"]["current"]["plan"] = {"artifact_id": "old-plan"}
    assert "fact_coverage_incomplete" in evaluate_acceptance(state, budget)["failures"]
    state.role_data["reviews"]["new"]["issue_closures"] = []
    assert "prior_issues_not_closed" in evaluate_acceptance(state, budget)["failures"]


def test_extractor_refuses_missing_or_ambiguous_current_report():
    from scripts.extract_e2e_final import select_artifacts

    repo, _ = setup_rework()
    repo.state.artifacts.pop("report")
    with pytest.raises(ValueError, match="No current report"):
        select_artifacts(repo.state)
    with pytest.raises(ValueError, match="explicit review"):
        select_artifacts(repo.state, review_id="nonexistent")


def test_timing_distinguishes_human_wait_from_parallel_model_work():
    from scripts.e2e_chain import timing_summary

    events = [
        {"kind": kind, "at": f"2026-10-30T00:{minute:02d}:00+00:00"}
        for kind, minute in [
            ("started", 0),
            ("paused", 1),
            ("resumed", 4),
            ("finished", 5),
        ]
    ]
    result = timing_summary(events, [{"elapsed_seconds": 90}], [{"seconds": 10}])
    assert result["wall_seconds"] == 300
    assert result["human_wait_seconds"] == 180
    assert result["model_cumulative_seconds"] == 90
