"""Comparisons reject incomplete/misbound data and expose per-case regressions."""

from copy import deepcopy

import pytest

from research_copilot.runtime.run_evaluation import Suite, digest
from tests.integration.test_run_evaluation import suite


def report():
    spec = suite()
    spec["cases"][0]["expected_signed"] = True
    spec["cases"].append({**deepcopy(spec["cases"][0]), "id": "units"})
    spec = Suite.model_validate(spec).model_dump(mode="json")
    rows = [
        {
            "case_id": case["id"],
            "budget_id": "standard",
            "scenario": case["scenario"],
            "observed": True,
            "report_id": case["id"],
            "run_id": "run-1",
            "policy_version": "policy-1",
            "budget_definition_sha256": "b" * 64,
            "snapshot_sha256": "c" * 64,
            "signed": True,
            "completed": True,
            "quality_passed": True,
            "usage_verified": True,
            "session_status": "completed",
            "checks": [{"id": "growth", "passed": True}],
            "calls": 2,
            "known_tokens": 80,
            "reserved_tokens": 0,
            "unknown_reserved_tokens": 0,
            "unknown_requests": 0,
        }
        for case in spec["cases"]
    ]
    return spec, {
        "format": 1,
        "scope": "stored_artifact_checks",
        "suite_sha256": digest(spec),
        "usage_origin": "synthetic_offline",
        "complete": True,
        "cases": rows,
        "budget_curve": [
            {
                **spec["budgets"][0],
                "expected_runs": 2,
                "observed_runs": 2,
                "coverage": 1,
                "quality_pass_rate": 1,
                "agent_completion_rate": 1,
                "calls": 4,
                "known_tokens": 160,
                "usage_complete": True,
            }
        ],
    }


def test_equal_totals_cannot_hide_case_regression_and_usage_is_ledger_only():
    from research_copilot.runtime.evaluation_comparison import compare_runs

    spec, before = report()
    before["cases"][1].update(quality_passed=False, signed=False, completed=False)
    before["cases"][1]["checks"][0]["passed"] = False
    after = deepcopy(before)
    after["cases"].reverse()
    for row in after["cases"]:
        succeeds = row["case_id"] == "units"
        row.update(
            quality_passed=succeeds,
            signed=succeeds,
            completed=succeeds,
            known_tokens=40,
        )
        row["checks"][0]["passed"] = succeeds
    result = compare_runs(spec, before, after)
    assert result["status"] == "regressed"
    assert result["real_model_cost_delta"] is None
    assert result["budget_curve"][0]["known_tokens_delta"] == -80
    assert result["budget_curve"][0]["quality_pass_rate_delta"] == 0
    assert result["cases"][0]["regressed_checks"] == ["growth"]
    assert result["cases"][0]["quality_regressed"]


@pytest.mark.parametrize(
    "change",
    ["missing", "unknown", "pending", "running", "budget", "origin", "suite", "policy"],
)
def test_incomparable_inputs_have_no_cost_delta_even_if_summary_claims_complete(change):
    from research_copilot.runtime.evaluation_comparison import compare_runs

    spec, before = report()
    after = deepcopy(before)
    row = after["cases"][0]
    if change == "missing":
        row.clear()
        row.update(
            case_id="period", budget_id="standard", observed=False, reason="not_run"
        )
    elif change == "unknown":
        row["usage_verified"] = False
    elif change == "pending":
        row["reserved_tokens"] = 100
    elif change == "running":
        row["session_status"] = "running"
    elif change == "budget":
        row["budget_definition_sha256"] = "d" * 64
    elif change == "origin":
        after["usage_origin"] = "recorded_runtime_unverified"
    elif change == "suite":
        after["suite_sha256"] = "d" * 64
    else:
        row["policy_version"] = "policy-2"
    result = compare_runs(spec, before, after)
    assert result["status"] == "not_comparable"
    assert result["reasons"]
    assert result["budget_curve"] == []


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "removed",
        "extra",
        "bad_type",
        "bad_count",
        "checks",
        "invented_success",
    ],
)
def test_corrupted_rows_cannot_be_used_as_a_baseline(change):
    from research_copilot.runtime.evaluation_comparison import compare_runs

    spec, before = report()
    after = deepcopy(before)
    if change == "duplicate":
        after["cases"].append(deepcopy(after["cases"][0]))
    elif change == "removed":
        after["cases"].pop()
    elif change == "extra":
        after["cases"][0]["case_id"] = "not-in-suite"
    elif change == "bad_type":
        after["cases"][0]["completed"] = "true"
    elif change == "bad_count":
        after["cases"][0]["known_tokens"] = -1
    elif change == "checks":
        after["cases"][0]["checks"] = []
    else:
        after["cases"][0]["checks"][0]["passed"] = False
    with pytest.raises(ValueError):
        compare_runs(spec, before, after)


def test_equal_comparison_ignores_untrusted_summary_totals_and_freezes_input_digest():
    from research_copilot.runtime.evaluation_comparison import compare_runs

    spec, before = report()
    after = deepcopy(before)
    after["budget_curve"][0].update(known_tokens=0, quality_pass_rate=0)
    result = compare_runs(spec, before, after)
    assert result["status"] == "no_regression"
    assert result["budget_curve"][0]["known_tokens_delta"] == 0
    assert result["baseline_sha256"] == digest(before)
    assert result["candidate_sha256"] == digest(after)


def test_duplicate_report_cannot_fill_two_distinct_case_slots():
    from research_copilot.runtime.evaluation_comparison import compare_runs

    spec, before = report()
    after = deepcopy(before)
    after["cases"][1]["report_id"] = after["cases"][0]["report_id"]
    with pytest.raises(ValueError, match="report"):
        compare_runs(spec, before, after)
