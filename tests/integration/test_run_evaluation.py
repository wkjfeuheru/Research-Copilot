"""Read-only evaluation uses actual stored rows; models/sources are never opened."""

from copy import deepcopy
from dataclasses import replace

import pytest


def suite():
    return {
        "format": 1,
        "cases": [
            {
                "id": "period",
                "scenario": "period_difference",
                "user_request": "",
                "expected_signed": False,
                "checks": [
                    {
                        "id": "growth",
                        "root": "metrics",
                        "path": [],
                        "match": "contains",
                        "expected": {"value": "20", "unit": "%"},
                    },
                ],
            },
        ],
        "budgets": [{"id": "standard", "token_limit": 1000000, "call_limit": None}],
    }


def bindings():
    return {
        "usage_origin": "synthetic_offline",
        "runs": [
            {
                "case_id": "period",
                "budget_id": "standard",
                "report_id": "report-1",
                "run_id": "run-1",
                "policy_version": "policy-1",
            }
        ],
    }


async def test_native_policy_branches_survive_export_and_comparison(role_env):
    from research_copilot.runtime.evaluation_comparison import compare_runs
    from research_copilot.runtime.preflight import load_execution_policy, new_budget
    from research_copilot.runtime.run_evaluation import evaluate_runs
    from research_copilot.runtime.session import SessionRecord

    _, _, store = role_env
    policy = load_execution_policy()
    await store.create(
        SessionRecord(
            "native-report",
            "actor-1",
            "v2",
            "policy-1",
            "native-budget",
            run_id="run-1",
            status="completed",
        ),
        new_budget("native-budget").snapshot(),
    )
    spec, runs = suite(), bindings()
    spec["budgets"][0].update(
        token_limit=policy["token_limit"],
        call_limit=policy["call_limit"],
        stage_limits=policy["stages"],
    )
    runs["runs"][0]["report_id"] = "native-report"
    result = await evaluate_runs(store, "actor-1", spec, runs)
    assert result["budget_curve"][0]["stage_limits"] == policy["stages"]
    assert compare_runs(spec, result, result)["status"] == "no_regression"

    changed = deepcopy(spec)
    changed["budgets"][0]["stage_limits"]["reviewer"]["branches"]["quality"] += 1
    with pytest.raises(ValueError):
        await evaluate_runs(store, "actor-1", changed, runs)
    assert compare_runs(changed, result, result)["status"] == "not_comparable"


async def test_missing_runs_remain_in_denominator_and_are_not_zero_cost(role_env):
    from research_copilot.runtime.run_evaluation import evaluate_runs

    _, _, store = role_env
    result = await evaluate_runs(
        store, "actor-1", suite(), {"usage_origin": "synthetic_offline", "runs": []}
    )
    row = result["budget_curve"][0]
    assert row["expected_runs"] == 1
    assert row["observed_runs"] == 0
    assert row["quality_pass_rate"] is None
    assert row["agent_completion_rate"] is None
    assert row["known_tokens"] is None
    assert not result["complete"]


async def test_reads_current_artifacts_only_and_keeps_unknown_usage(role_env):
    from research_copilot.contracts import ArtifactRef
    from research_copilot.runtime.run_evaluation import evaluate_runs
    from tests.unit.test_orchestration import context

    _, _, store = role_env
    ref = ArtifactRef(
        artifact_id="analysis-current", version="1", content_hash="a" * 64
    )

    def seed(state):
        state.artifacts["analysis"] = ref
        state.role_data["objects"] = {
            ref.artifact_id: {
                "ref": ref.model_dump(),
                "dispatch_id": "dispatch-current",
                "payload": {"metric_ids": ["M-current"], "evidence_ids": []},
            }
        }
        state.role_data["metrics"] = {
            "dispatch-old": {"M-old": {"value": "20", "unit": "%"}},
            "dispatch-current": {"M-current": {"value": "21", "unit": "%"}},
        }

    await store.mutate_control(context(), seed)
    budget = await store.load_budget(context())
    reservation = await budget.reserve(100)
    await budget.mark_sent(reservation)
    before = await store.read_snapshot("report-1", "actor-1")
    result = await evaluate_runs(store, "actor-1", suite(), bindings())
    row = result["cases"][0]
    assert row["checks"] == [{"id": "growth", "passed": False}]
    assert row["usage_verified"] is False
    assert row["reserved_tokens"] == 100
    assert row["real_model_cost"] is None
    assert result["budget_curve"][0]["quality_pass_rate"] == 0
    after = await store.read_snapshot("report-1", "actor-1")
    assert before.record == after.record
    assert before.state == after.state
    assert before.budget.snapshot() == after.budget.snapshot()


@pytest.mark.parametrize(
    "change",
    ["actor", "question", "run", "policy", "limit", "stage_limit", "duplicate"],
)
async def test_refuses_misbound_runs(role_env, change):
    from research_copilot.runtime.run_evaluation import evaluate_runs

    _, _, store = role_env
    spec, runs, actor = suite(), bindings(), "actor-1"
    if change == "actor":
        actor = "other"
    elif change == "question":
        spec["cases"][0]["user_request"] = "different case"
    elif change == "run":
        runs["runs"][0]["run_id"] = "old-run"
    elif change == "policy":
        runs["runs"][0]["policy_version"] = "old-policy"
    elif change == "limit":
        spec["budgets"][0]["token_limit"] = 10
    elif change == "stage_limit":
        spec["budgets"][0]["stage_limits"] = {"planner": {"calls": 1, "tokens": 100}}
    else:
        runs["runs"].append(deepcopy(runs["runs"][0]))
    with pytest.raises((ValueError, PermissionError, LookupError)):
        await evaluate_runs(store, actor, spec, runs)


async def test_missing_path_fails_even_when_expected_null_and_unsigned_is_not_complete(
    role_env,
):
    from research_copilot.runtime.run_evaluation import evaluate_runs

    _, _, store = role_env
    spec = suite()
    spec["cases"][0]["checks"] = [
        {"id": "missing", "root": "report", "path": ["absent"], "expected": None}
    ]
    record = await store.load("report-1", "actor-1")
    await store.save(replace(record, status="paused"))
    result = await evaluate_runs(store, "actor-1", spec, bindings())
    assert not result["cases"][0]["checks"][0]["passed"]
    assert result["budget_curve"][0]["agent_completion_rate"] == 0


@pytest.mark.parametrize(
    "change",
    ["empty", "unknown_root", "duplicate_check", "unknown_case", "unknown_origin"],
)
async def test_invalid_manifests_rejected_before_database_access(change):
    from research_copilot.runtime.run_evaluation import evaluate_runs

    spec, runs = suite(), bindings()
    if change == "empty":
        spec["cases"] = []
    elif change == "unknown_root":
        spec["cases"][0]["checks"][0]["root"] = "secrets"
    elif change == "duplicate_check":
        spec["cases"][0]["checks"] *= 2
    elif change == "unknown_case":
        runs["runs"][0]["case_id"] = "unknown"
    else:
        runs["usage_origin"] = "real_verified"
    with pytest.raises(ValueError):
        await evaluate_runs(None, "actor-1", spec, runs)


async def test_signed_case_uses_shared_eligibility_and_observed_ledger_cost(role_env):
    from research_copilot.runtime.business import approval_key
    from research_copilot.runtime.run_evaluation import evaluate_runs
    from tests.integration.test_signoff_eligibility import ready_report
    from tests.unit.test_orchestration import context

    _, business, store = role_env
    report = await ready_report(store, human=True)
    ledger = await store.load_budget(context())
    request = await ledger.reserve(100)
    await ledger.mark_sent(request)
    await ledger.settle(request, 13)
    await business.confirm(
        context(),
        "signoff_report",
        report,
        approval_key("report-1", "signoff_report", report),
    )
    spec = suite()
    spec["cases"][0].update(
        expected_signed=True,
        checks=[
            {
                "id": "review",
                "root": "review",
                "path": ["checks_complete"],
                "expected": True,
            }
        ],
    )
    spec["cases"].append({**deepcopy(spec["cases"][0]), "id": "other"})
    result = await evaluate_runs(store, "actor-1", spec, bindings())
    curve = result["budget_curve"][0]
    assert (
        curve["coverage"]
        == curve["quality_pass_rate"]
        == curve["agent_completion_rate"]
        == 0.5
    )
    assert curve["known_tokens"] == 13 and curve["calls"] == 1
    assert not curve["usage_complete"]
    assert result["cases"][0]["quality_passed"]
    assert result["cases"][0]["estimated_cost"] is None
    unknown = await ledger.reserve(100)
    await ledger.mark_sent(unknown)
    updated = await evaluate_runs(store, "actor-1", spec, bindings())
    assert not updated["cases"][0]["completed"]
    assert (
        updated["cases"][0]["snapshot_sha256"] != result["cases"][0]["snapshot_sha256"]
    )


async def test_current_analysis_facts_require_plan_scope_and_citation(role_env):
    from research_copilot.contracts import ArtifactRef
    from research_copilot.runtime.run_evaluation import evaluate_runs
    from tests.unit.test_orchestration import context

    _, _, store = role_env
    ref = ArtifactRef(artifact_id="a", version="1", content_hash="a" * 64)

    def seed(state):
        state.artifacts["analysis"] = ref
        state.role_data.update(
            {
                "objects": {
                    "a": {
                        "ref": ref.model_dump(),
                        "dispatch_id": "d",
                        "payload": {
                            "evidence_ids": ["e"],
                            "verified_facts": [
                                {"input_ref": {"key": key, "evidence_id": "e"}}
                                for key in ("good", "wrong-plan", "outside")
                            ],
                        },
                    }
                },
                "scopes": {"d": {"fact_sheet_ids": ["good", "wrong-plan"]}},
                "fact_sheets": {
                    key: {
                        "plan": state.artifacts["plan"].model_dump()
                        if key != "wrong-plan"
                        else {},
                        "facts": [{"input_ref": {"key": key, "evidence_id": "e"}}],
                    }
                    for key in ("good", "wrong-plan", "outside")
                },
            }
        )

    await store.mutate_control(context(), seed)
    spec = suite()
    spec["cases"][0]["checks"] = [
        {
            "id": key,
            "root": "facts",
            "path": [],
            "match": "contains",
            "expected": {"input_ref": {"key": key}},
        }
        for key in ("good", "wrong-plan", "outside")
    ]
    result = await evaluate_runs(store, "actor-1", spec, bindings())
    assert [check["passed"] for check in result["cases"][0]["checks"]] == [
        True,
        False,
        False,
    ]


async def test_cannot_forge_run_origin_using_manifest_label(role_env):
    from research_copilot.runtime.run_evaluation import evaluate_runs

    _, _, store = role_env
    bound = bindings()
    bound["usage_origin"] = "recorded_runtime_unverified"
    result = await evaluate_runs(store, "actor-1", suite(), bound)
    assert result["real_model_cost"] is None
    assert result["scope"] == "stored_artifact_checks"


async def test_exact_checks_distinguish_nested_booleans_from_numbers(role_env):
    from research_copilot.runtime.run_evaluation import evaluate_runs
    from tests.integration.test_signoff_eligibility import ready_report
    from tests.unit.test_orchestration import context

    _, _, store = role_env
    await ready_report(store)
    await store.mutate_control(
        context(),
        lambda state: state.role_data["objects"]["report"]["payload"].update(
            flag={"value": 1}
        ),
    )
    spec = suite()
    spec["cases"][0]["checks"] = [
        {"id": "typed", "root": "report", "path": ["flag"], "expected": {"value": True}}
    ]
    result = await evaluate_runs(store, "actor-1", spec, bindings())
    assert not result["cases"][0]["checks"][0]["passed"]


async def test_comparison_consumes_real_exported_rows_and_reports_added_ledger_usage(
    role_env,
):
    from research_copilot.runtime.business import approval_key
    from research_copilot.runtime.evaluation_comparison import compare_runs
    from research_copilot.runtime.run_evaluation import evaluate_runs
    from tests.integration.test_signoff_eligibility import ready_report
    from tests.unit.test_orchestration import context

    _, business, store = role_env
    report = await ready_report(store, human=True)
    await business.confirm(
        context(),
        "signoff_report",
        report,
        approval_key("report-1", "signoff_report", report),
    )
    record = await store.load("report-1", "actor-1")
    await store.save(replace(record, status="completed"))
    spec = suite()
    spec["cases"][0].update(
        expected_signed=True,
        checks=[
            {
                "id": "review",
                "root": "review",
                "path": ["checks_complete"],
                "expected": True,
            }
        ],
    )
    before = await evaluate_runs(store, "actor-1", spec, bindings())
    budget = await store.load_budget(context())
    reservation = await budget.reserve(10)
    await budget.mark_sent(reservation)
    await budget.settle(reservation, 7)
    after = await evaluate_runs(store, "actor-1", spec, bindings())
    comparison = compare_runs(spec, before, after)
    assert comparison["status"] == "no_regression"
    assert comparison["cases"][0]["known_tokens_delta"] == 7
    assert comparison["budget_curve"][0]["calls_delta"] == 1
    assert comparison["real_model_cost_delta"] is None
