"""Declarative metric spec path: shared arithmetic, requirements and the sandbox runner."""

import importlib.util
import json
from pathlib import Path

import pytest

from research_copilot.subagents.metric_quality import (
    compute_expected,
    rounding_error,
    validate_metric_batch,
    validate_metric_semantics,
)

SPEC_RUNNER = (
    Path(__file__).resolve().parents[2]
    / "src/research_copilot/resources/skills/analysist/"
    "financial-modeling/scripts/spec_runner.py"
)


def load_spec_runner():
    spec = importlib.util.spec_from_file_location("spec_runner", SPEC_RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def spec_metric(key, value, inputs, **overrides):
    entry = {
        "key": key,
        "value": value,
        "unit": "%",
        "semantic_type": "growth",
        "formula_id": "growth_percent",
        "input_keys": inputs,
        "numerator_basis": "current",
        "denominator_basis": "base",
        "display_precision": 2,
        "is_estimate": False,
        "description": "growth",
    }
    entry.update(overrides)
    return entry


def test_compute_expected_matches_validate_for_every_formula():
    from decimal import Decimal

    operands = {"a": {"raw_value": "120.00"}, "b": {"raw_value": "100"}}
    for formula, expected in (
        ("difference", "20"),
        ("product", "12000"),
        ("ratio", "1.2"),
        ("growth_percent", "20"),
    ):
        assert compute_expected(formula, Decimal(120), Decimal(100)) == Decimal(
            expected
        )
        metric = spec_metric(
            "m",
            expected,
            ["a", "b"],
            formula_id=formula,
            unit="x" if formula != "growth_percent" else "%",
        )
        derived = validate_metric_semantics(metric, operands)
        assert derived["key"] == "m"
    with pytest.raises(ValueError, match="Unsupported formula_id"):
        compute_expected("net_margin_contribution", Decimal(1), Decimal(2))


def test_ratio_declared_in_percent_is_registered_as_percentage():
    operands = {
        "profit": {"raw_value": "50744682"},
        "revenue": {"raw_value": "362012554"},
    }
    metric = spec_metric(
        "net_margin",
        "14.02",
        ["profit", "revenue"],
        formula_id="ratio",
        unit="%",
        semantic_type="net_margin",
    )
    assert validate_metric_semantics(metric, operands)["value"] == "14.02"
    with pytest.raises(ValueError, match="formula/value mismatch"):
        validate_metric_semantics({**metric, "value": "0.14"}, operands)


def test_spec_runner_registers_percent_ratio_as_percentage(tmp_path, monkeypatch):
    runner = load_spec_runner()
    document = {
        "schema_version": 1,
        "inputs": {
            "profit": {"raw_value": "50744682"},
            "revenue": {"raw_value": "362012554"},
        },
        "assumptions": {},
        "metrics": [
            spec_metric(
                "net_margin",
                "14.02",
                ["profit", "revenue"],
                formula_id="ratio",
                unit="%",
                semantic_type="net_margin",
            )
        ],
    }
    spec_path = tmp_path / "spec_inputs.json"
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("RC_OUTPUT_DIR", str(tmp_path / "out"))
    runner.main(["spec_runner.py", str(spec_path)])
    result = json.loads((tmp_path / "out/result.json").read_text("utf-8"))
    assert result["metrics"][0]["value"] == "14.02"
    assert result["metrics"][0]["unit"] == "%"


def test_impossible_margin_percentage_is_rejected_with_formula_hint():
    metric = spec_metric(
        "gross_margin_2024",
        "132.60",
        ["revenue", "cost"],
        formula_id="ratio",
        unit="%",
        semantic_type="毛利率",
    )
    with pytest.raises(ValueError, match="margin.*-100..100|difference then ratio"):
        validate_metric_semantics(
            metric, {"revenue": {"raw_value": "362"}, "cost": {"raw_value": "273"}}
        )


def test_rate_difference_requirement_accepts_plan_synonyms():
    metrics = [
        spec_metric(
            "gm_change",
            "5.25",
            ["gm24", "gm23"],
            formula_id="difference",
            unit="pp",
            semantic_type="rate_difference",
        )
    ]
    inputs = {"gm24": {"value": "24.44"}, "gm23": {"value": "19.19"}}
    validated = validate_metric_batch(
        metrics,
        inputs,
        requirements=[{"semantic_type": "百分点变动", "unit": "百分点"}],
    )
    assert validated["gm_change"]["value"] == "5.25"


def test_spec_runner_chains_full_precision_values(tmp_path, monkeypatch):
    runner = load_spec_runner()
    document = {
        "schema_version": 1,
        "inputs": {
            "profit24": {"raw_value": "88.493595"},
            "revenue24": {"raw_value": "362.012554"},
            "profit23": {"raw_value": "76.934915"},
            "revenue23": {"raw_value": "400.917045"},
        },
        "assumptions": {},
        "metrics": [
            spec_metric(
                "gm24",
                "24.44",
                ["profit24", "revenue24"],
                formula_id="ratio",
                unit="%",
                semantic_type="毛利率",
            ),
            spec_metric(
                "gm23",
                "19.19",
                ["profit23", "revenue23"],
                formula_id="ratio",
                unit="%",
                semantic_type="毛利率",
            ),
            spec_metric(
                "gm_change",
                "5.25",
                ["gm24", "gm23"],
                formula_id="difference",
                unit="pp",
                semantic_type="rate_difference",
            ),
        ],
    }
    spec_path = tmp_path / "spec_inputs.json"
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("RC_OUTPUT_DIR", str(tmp_path / "out"))
    runner.main(["spec_runner.py", str(spec_path)])
    result = json.loads((tmp_path / "out/result.json").read_text("utf-8"))
    by_key = {item["key"]: item for item in result["metrics"]}
    assert by_key["gm24"]["value"] == "24.44"
    assert by_key["gm23"]["value"] == "19.19"
    # The difference of the full-precision margins rounds to 5.26.
    assert by_key["gm_change"]["value"] == "5.26"
    validate_metric_batch(result["metrics"], document["inputs"])


def test_requirements_match_semantics_without_pinned_value():
    metrics = [spec_metric("growth_2024", "7.09", ["rev_2024", "rev_2023"])]
    inputs = {
        "rev_2024": {"raw_value": "891.75", "evidence_id": "E-000001"},
        "rev_2023": {"raw_value": "832.72", "evidence_id": "E-000001"},
    }
    validated = validate_metric_batch(
        metrics,
        inputs,
        requirements=[{"semantic_type": "growth", "unit": "%"}],
        require_fact_lineage=True,
    )
    assert validated["growth_2024"]["value"] == "7.09"
    with pytest.raises(ValueError, match="Missing required metric"):
        validate_metric_batch(
            metrics,
            inputs,
            requirements=[{"semantic_type": "rate_difference", "unit": "pp"}],
            require_fact_lineage=True,
        )


def test_rounding_error_helper_propagates_multiplication():
    assert rounding_error("1.00", 2) == "0.005"


def test_spec_runner_roundtrip_through_registration_validation(tmp_path, monkeypatch):
    runner = load_spec_runner()
    inputs = {
        "revenue_2024": {
            "key": "revenue_2024",
            "evidence_id": "E-000001",
            "content_hash": "a" * 64,
            "pointer": "/revenue",
            "raw_value": "832.72",
            "unit": "CNY亿元",
            "currency": "CNY",
            "period": "2023",
            "entity": "五粮液",
            "basis": "consolidated",
            "scale": "1",
            "transformation": "identity",
        },
        "revenue_2025": {
            "key": "revenue_2025",
            "evidence_id": "E-000001",
            "content_hash": "a" * 64,
            "pointer": "/revenue",
            "raw_value": "891.75",
            "unit": "CNY亿元",
            "currency": "CNY",
            "period": "2024",
            "entity": "五粮液",
            "basis": "consolidated",
            "scale": "1",
            "transformation": "identity",
        },
    }
    document = {
        "schema_version": 1,
        "inputs": inputs,
        "assumptions": {
            "tax_rate": {
                "value": "0.25",
                "basis": "法定税率假设",
                "impact": "仅影响税后中间量",
            }
        },
        "metrics": [
            spec_metric("revenue_growth", "7.09", ["revenue_2025", "revenue_2024"]),
            spec_metric(
                "expensed_share",
                "1.77",
                ["tax_rate", "revenue_growth"],
                formula_id="product",
                unit="x",
                is_estimate=True,
                semantic_type="inferred_amount",
            ),
        ],
    }
    spec_path = tmp_path / "spec_inputs.json"
    spec_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    output_dir = tmp_path / "outputs"
    monkeypatch.setenv("RC_OUTPUT_DIR", str(output_dir))
    runner.main(["spec_runner.py", str(spec_path)])
    result = json.loads((output_dir / "result.json").read_text("utf-8"))
    assert result["schema_version"] == 2
    by_key = {item["key"]: item for item in result["metrics"]}
    assert by_key["revenue_growth"]["value"] == "7.09"
    # The cascade metric inherits the estimate flag from its assumption input.
    assert by_key["expensed_share"]["is_estimate"] is True
    assert "tax_rate" in result["assumptions"]
    assert {ref["key"] for ref in result["input_refs"]} == {
        "revenue_2024",
        "revenue_2025",
    }
    operands = {
        **inputs,
        **{
            key: {"assumption_key": key, "is_estimate": True, **value}
            for key, value in result["assumptions"].items()
        },
    }
    validated = validate_metric_batch(
        result["metrics"], operands, require_fact_lineage=True
    )
    assert validated["expensed_share"]["value"] == "1.77"
    # A drifted value cannot pass registration validation.
    drifted = [dict(item, value="9.99") for item in result["metrics"]]
    with pytest.raises(ValueError, match="formula/value mismatch"):
        validate_metric_batch(drifted, operands, require_fact_lineage=True)


def test_spec_runner_rejects_unknown_inputs_and_duplicate_keys(tmp_path, monkeypatch):
    runner = load_spec_runner()
    monkeypatch.setenv("RC_OUTPUT_DIR", str(tmp_path / "out"))
    document = {
        "schema_version": 1,
        "inputs": {},
        "assumptions": {},
        "metrics": [spec_metric("m", "1", ["missing", "also_missing"])],
    }
    spec_path = tmp_path / "spec_inputs.json"
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown inputs"):
        runner.main(["spec_runner.py", str(spec_path)])


def test_intermediate_only_spec_batch_is_rejected_before_execution():
    from research_copilot.subagents.analysist import AnalysisService

    service = AnalysisService(None)
    specs = [
        spec_metric(
            "gross_profit",
            "20",
            ["revenue", "cost"],
            semantic_type="毛利额",
            unit="元",
            formula_id="difference",
        )
    ]
    required = [{"key": "margin_q2", "semantic_type": "毛利率", "unit": "%"}]
    with pytest.raises(ValueError, match="margin_q2") as raised:
        service._validate_specs(
            specs, set(), {"revenue", "cost"}, requirements=required
        )
    # 报错须带上计划口径，否则模型只能逐条试错（第十一轮两次整批被拒）。
    assert "semantic_type=毛利率" in str(raised.value)
    assert "unit=%" in str(raised.value)
