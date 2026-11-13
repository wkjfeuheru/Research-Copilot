from pathlib import Path

from research_copilot.runtime.quality_baseline import evaluate_suite


async def test_frozen_cross_case_baseline_reports_quality_and_explicit_synthetic_cost():
    result = await evaluate_suite(Path("tests/fixtures/engineering_baseline"))
    assert result["case_count"] >= 6
    assert result["contract_accuracy"] == 1
    assert result["usage_origin"] == "synthetic_offline"
    assert result["real_model_cost"] is None
    assert {row["scenario"] for row in result["cases"]} >= {
        "period_difference",
        "unit_conversion",
        "missing_evidence",
        "conflicting_sources",
        "chart_review",
    }
    assert result["replayed_calls"] > 0 and result["replayed_tokens"] > 0
    assert (
        result["budget_curve"][0]["completed_replays"]
        < result["budget_curve"][-1]["completed_replays"]
    )
