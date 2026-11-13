"""CATL regression: independently calculated quarter change, never rounded operands."""

import json
from decimal import Decimal

import pytest

from research_copilot.subagents.metric_quality import validate_metric_batch
from tests.unit.test_metric_specs import load_spec_runner, spec_metric


def quarter_chain():
    inputs = {
        key: {"raw_value": value}
        for key, value in {
            "revenue3": "165220844000",
            "cost3": "131040123000",
            "revenue2": "147785539000",
            "cost2": "113568520000",
        }.items()
    }
    metrics = []
    for q, profit, margin in [
        ("3", "34180721000", "20.69"),
        ("2", "34217019000", "23.15"),
    ]:
        metrics.extend(
            [
                spec_metric(
                    "profit" + q,
                    profit,
                    ["revenue" + q, "cost" + q],
                    formula_id="difference",
                    unit="元",
                    semantic_type="amount",
                ),
                spec_metric(
                    "margin" + q,
                    margin,
                    ["profit" + q, "revenue" + q],
                    formula_id="ratio",
                    unit="%",
                    semantic_type="gross_margin",
                ),
            ]
        )
    metrics.append(
        spec_metric(
            "change",
            "-2.47",
            ["margin3", "margin2"],
            formula_id="difference",
            unit="pp",
            semantic_type="rate_difference",
        )
    )
    return inputs, metrics


def test_host_recomputes_chain_without_intermediate_display_rounding():
    inputs, metrics = quarter_chain()
    registered = validate_metric_batch(metrics, inputs)
    assert registered["change"]["value"] == "-2.47"
    assert abs(
        Decimal(registered["change"]["calculated_value"])
        - Decimal("-2.46525896795012029353033093")
    ) < Decimal("1e-24")
    metrics[-1]["value"] = "-2.46"
    with pytest.raises(ValueError, match="formula/value mismatch"):
        validate_metric_batch(metrics, inputs)


def test_fixed_runner_preserves_calculation_precision(tmp_path, monkeypatch):
    inputs, metrics = quarter_chain()
    source = tmp_path / "spec.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "inputs": inputs,
                "assumptions": {},
                "metrics": metrics,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RC_OUTPUT_DIR", str(tmp_path / "out"))
    load_spec_runner().main(["runner", str(source)])
    result = json.loads((tmp_path / "out/result.json").read_text("utf-8"))
    assert result["metrics"][-1]["value"] == "-2.47"
    validate_metric_batch(result["metrics"], inputs)


def comparison(**changes):
    return {
        "current_period": "2026Q3",
        "base_period": "2026Q2",
        "period_basis": "quarter",
        "entity": "CATL",
        "reporting_scope": "consolidated",
        "denominator": "total_revenue",
        "mode": "actual",
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {
            "current_period": "2026H1",
            "base_period": "2025H1",
            "period_basis": "half_year",
        },
        {"denominator": "battery_revenue"},
        {"reporting_scope": "parent"},
        {"mode": "scenario"},
    ],
)
def test_residual_rejects_incomparable_effects(change):
    inputs = {
        "delta": {"value": "-2.47", "comparison": comparison()},
        "mix": {"value": "-0.08", "comparison": comparison(**change)},
    }
    metric = spec_metric(
        "residual",
        "-2.39",
        ["delta", "mix"],
        formula_id="difference",
        semantic_type="attribution_residual",
        unit="pp",
        comparison=comparison(),
    )
    with pytest.raises(ValueError, match="comparison"):
        validate_metric_batch([metric], inputs)


def test_attribution_requires_verified_comparison_and_known_source_basis():
    metric = spec_metric(
        "mix_effect",
        "0.1",
        ["share", "margin"],
        formula_id="custom",
        semantic_type="mix_effect",
        unit="pp",
    )
    inputs = {
        "share": {"value": "0.1", "currency": "unknown", "basis": "unknown"},
        "margin": {"value": "1"},
    }
    with pytest.raises(ValueError, match="comparison"):
        validate_metric_batch([metric], inputs)
    metric["comparison"] = comparison(reporting_scope="unknown")
    with pytest.raises(ValueError, match="comparison"):
        validate_metric_batch([metric], inputs)


def test_same_period_bridge_is_accepted():
    inputs = {
        "delta": {"value": "-2.47", "comparison": comparison()},
        "mix": {"value": "-0.08", "comparison": comparison()},
    }
    metric = spec_metric(
        "residual",
        "-2.39",
        ["delta", "mix"],
        formula_id="difference",
        semantic_type="attribution_residual",
        unit="pp",
        comparison=comparison(),
    )
    assert validate_metric_batch([metric], inputs)["residual"]["value"] == "-2.39"


@pytest.mark.parametrize("location", ["comparison", "source"])
def test_undisclosed_scope_cannot_be_used_for_actual_attribution(location):
    inputs = {
        "delta": {"value": "-2.47", "basis": "合并"},
        "mix": {"value": "-0.08", "basis": "合并"},
    }
    basis = comparison()
    if location == "comparison":
        basis["reporting_scope"] = "未标明"
    else:
        inputs["mix"]["basis"] = "报表范围未标明"
    metric = spec_metric(
        "residual",
        "-2.39",
        ["delta", "mix"],
        formula_id="difference",
        semantic_type="attribution_residual",
        unit="pp",
        comparison=basis,
    )
    with pytest.raises(ValueError, match="unknown"):
        validate_metric_batch([metric], inputs)


def test_old_rounded_receipts_cannot_be_republished():
    from research_copilot.subagents import metric_quality

    inputs, metrics = quarter_chain()
    metrics[-1]["value"] = "-2.46"
    registered = {
        m["key"]: {
            **m,
            "local_key": m["key"],
            "execution_id": "old",
            "inputs": [{"key": k, **v} for k, v in inputs.items()],
            "calculated_value": m["value"],
        }
        for m in metrics
    }
    checker = getattr(metric_quality, "validate_registered_metrics", None)
    assert checker is not None, (
        "submission and review need independent receipt recomputation"
    )
    with pytest.raises(ValueError, match="formula/value mismatch"):
        checker(registered, ["change"])


def test_source_null_currency_is_preserved_as_unknown():
    from types import SimpleNamespace
    from research_copilot.subagents.contracts import SearchResult
    from research_copilot.subagents.facts import validate_facts
    from research_copilot.subagents.services import digest

    raw = {"items": [{"revenue": "100", "currency": None, "unit": "元"}]}
    evidence = {
        "raw": raw,
        "content_hash": digest(raw),
        "source": {"server": "test"},
        "collected_at": "2026-11-12",
        "kind": "financial_data",
    }
    state = SimpleNamespace(role_data={"evidence": {"E-000001": evidence}})
    scope = {"evidence_ids": ["E-000001"]}
    value = {
        "task_id": "s",
        "status": "completed",
        "summary": "found",
        "evidence_ids": ["E-000001"],
        "facts": [
            {
                "key": "revenue",
                "evidence_id": "E-000001",
                "content_hash": digest(raw),
                "pointer": "/items/0/revenue",
                "quote": "100",
                "raw_value": "100",
                "unit": "元",
                "currency": "CNY",
                "period": "2026H1",
                "entity": "CATL",
                "basis": "unknown",
            }
        ],
    }
    with pytest.raises(ValueError, match="currency"):
        validate_facts(SearchResult.model_validate(value), state, scope)
    value["facts"][0]["currency"] = "unknown"
    assert (
        validate_facts(SearchResult.model_validate(value), state, scope)[0][
            "input_ref"
        ]["currency"]
        == "unknown"
    )
