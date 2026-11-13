"""Frozen r4 negatives; no dependency on the original run directory or a provider."""

import hashlib
import json
from pathlib import Path

import pytest
from research_copilot.subagents.quality import (
    assess_source,
    validate_claims,
    validate_metric_semantics,
)

ROOT = Path(__file__).parents[1] / "fixtures/wuliangye_r4"


def read(name):
    return json.loads((ROOT / name).read_text("utf-8"))


def unproject(value):
    if isinstance(value, dict):
        if set(value) == {"join", "text_segments"}:
            return value["join"].join(value["text_segments"])
        return {k: unproject(v) for k, v in value.items()}
    return [unproject(v) for v in value] if isinstance(value, list) else value


def test_frozen_fixture_hashes_and_r4_bad_semantic():
    for name, expected in read("provenance.json")["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    bad = next(
        m
        for m in read("metrics.json")
        if m["local_key"] == "net_margin_impact_pretax_pp"
    )
    candidate = {
        **bad,
        "key": bad["local_key"],
        "semantic_type": "net_margin_contribution",
        "formula_id": "difference",
        "numerator_basis": "rates",
        "denominator_basis": "revenue",
        "is_estimate": False,
        "display_precision": 2,
    }
    with pytest.raises(ValueError, match="rate_difference"):
        validate_metric_semantics(candidate, {})


def test_r4_late_web_sources_are_ineligible_but_controlled_input_keeps_unknown_pit():
    sources = [unproject(s["value"]) for s in read("sources.json")]
    for source in sources:
        decision = assess_source(source, "2025-04-30")
        if source["kind"] == "attachment_original":
            assert (
                decision["eligible"]
                and decision["historical_availability"] == "unknown"
            )
        else:
            assert not decision["eligible"]


def test_missing_estimate_and_missing_growth_gap_are_blocked_and_corrected_example_passes():
    metric = {
        "M": {
            "value": "5.2398",
            "unit": "ratio",
            "is_estimate": True,
            "display_precision": 2,
        },
        "G": {
            "value": "1.65",
            "unit": "pp",
            "is_estimate": False,
            "display_precision": 2,
        },
    }
    conclusions = [
        {"claim_id": "growth_gap", "metric_ids": ["G"], "required_in_report": True}
    ]
    body = "销售费用增速为收入增速的5.24倍。"

    def manifest(text, claims):
        return {
            "body_hash": hashlib.sha256(text.encode()).hexdigest(),
            "claims": claims,
        }

    claim = {
        "quote": body,
        "start": 0,
        "end": len(body),
        "metric_ids": ["M"],
        "evidence_ids": [],
    }
    errors = validate_claims(body, manifest(body, [claim]), metric, {}, conclusions)
    assert any("estimate" in e for e in errors)
    assert any("required conclusion" in e for e in errors)
    body = (
        "销售费用增速为收入增速的5.24倍（推算），收入与归母净利润增速相差1.65个百分点。"
    )
    claim.update(
        quote=body, end=len(body), metric_ids=["M", "G"], conclusion_id="growth_gap"
    )
    assert validate_claims(body, manifest(body, [claim]), metric, {}, conclusions) == []


def test_metric_dependencies_propagate_estimate():
    from research_copilot.sandbox.contracts import ResultManifest

    common = {
        "unit": "ratio",
        "semantic_type": "inferred_amount",
        "formula_id": "product",
        "numerator_basis": "amount",
        "denominator_basis": "amount",
        "is_estimate": False,
        "display_precision": 2,
    }
    first = {**common, "key": "estimated", "value": "6", "input_keys": ["a", "b"]}
    second = {
        **common,
        "semantic_type": "ratio",
        "key": "derived",
        "value": "12",
        "input_keys": ["estimated", "b"],
    }
    operands = {"a": {"raw_value": "3"}, "b": {"raw_value": "2"}}
    operands["estimated"] = validate_metric_semantics(first, operands)
    assert validate_metric_semantics(second, operands)["is_estimate"]
    with pytest.raises(ValueError, match="cyclic"):
        ResultManifest.model_validate(
            {
                "schema_version": 2,
                "input_refs": [],
                "metrics": [second, first],
                "charts": [],
            }
        )
