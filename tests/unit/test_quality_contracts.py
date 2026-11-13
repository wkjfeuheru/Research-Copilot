import hashlib
import importlib
import pytest


def quality():
    return importlib.import_module("research_copilot.subagents.quality")


def test_rate_spread_cannot_be_relabelled_net_margin_and_estimates_propagate():
    q = quality()
    with pytest.raises(ValueError, match="rate_difference"):
        q.validate_metric_semantics(
            {
                "key": "net_margin_impact_pretax_pp",
                "value": "-1.37",
                "semantic_type": "net_margin_contribution",
                "formula_id": "difference",
                "input_keys": ["a", "b"],
                "unit": "pp",
                "display_precision": 2,
                "numerator_basis": "rate",
                "denominator_basis": "revenue",
                "is_estimate": False,
            },
            {"a": {"raw_value": "1.26"}, "b": {"raw_value": "2.63"}},
        )
    metric = {
        "key": "spread",
        "value": "-1.37",
        "semantic_type": "rate_difference",
        "formula_id": "difference",
        "input_keys": ["a", "b"],
        "unit": "pp",
        "display_precision": 2,
        "numerator_basis": "rate",
        "denominator_basis": "revenue",
        "is_estimate": False,
    }
    assert (
        q.validate_metric_semantics(
            metric,
            {
                "a": {"raw_value": "1.26"},
                "b": {"raw_value": "2.63", "is_estimate": True},
            },
        )["is_estimate"]
        is True
    )
    metric["value"] = "99"
    with pytest.raises(ValueError, match="formula"):
        q.validate_metric_semantics(
            metric, {"a": {"raw_value": "1.26"}, "b": {"raw_value": "2.63"}}
        )


def test_claim_removal_stale_text_and_unmarked_estimate_fail():
    q = quality()
    body = "销售费用77.94亿元（推算）。"
    metrics = {
        "M-1": {
            "value": "77.94",
            "unit": "亿元",
            "is_estimate": True,
            "display_precision": 2,
        }
    }
    manifest = {
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "claims": [
            {
                "quote": body,
                "start": 0,
                "end": len(body),
                "metric_ids": ["M-1"],
                "evidence_ids": [],
                "conclusion_id": "expenses",
            }
        ],
    }
    required = [
        {
            "claim_id": "expenses",
            "required_in_report": True,
            "metric_ids": ["M-1"],
            "evidence_ids": [],
        }
    ]
    assert q.validate_claims(body, manifest, metrics, {}, required) == []
    assert q.validate_claims(body, {**manifest, "claims": []}, metrics, {}, required)
    assert q.validate_claims(body + "新增", manifest, metrics, {}, required)
    plain = body.replace("（推算）", "")
    unmarked = {
        "body_hash": hashlib.sha256(plain.encode()).hexdigest(),
        "claims": [{**manifest["claims"][0], "quote": plain, "end": len(plain)}],
    }
    assert any(
        "estimate" in e
        for e in q.validate_claims(plain, unmarked, metrics, {}, required)
    )


def test_estimate_marker_may_sit_anywhere_in_the_claim_quote():
    q = quality()
    body = "销售费用77.94亿元，较上年增加，该比例为按公开数据推算的结果。"
    metrics = {
        "M-1": {
            "value": "77.94",
            "unit": "亿元",
            "is_estimate": True,
            "display_precision": 2,
        }
    }
    manifest = {
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "claims": [
            {
                "quote": body,
                "start": 0,
                "end": len(body),
                "metric_ids": ["M-1"],
                "evidence_ids": [],
            }
        ],
    }
    assert q.validate_claims(body, manifest, metrics, {}, []) == []


def test_dates_tickers_and_headings_are_not_financial_claims():
    q = quality()
    text = "# 五粮液000858.SZ 2024年报\n## 1. 结论\n基准日2025-04-30。"
    assert (
        q.validate_claims(
            text,
            {"body_hash": hashlib.sha256(text.encode()).hexdigest(), "claims": []},
            {},
            {},
            [],
        )
        == []
    )


def test_source_date_is_distinct_from_controlled_package_pit_unknown():
    q = quality()
    assert not q.assess_source(
        {"kind": "search_snippet", "raw": {"published_at": "2025-05-01"}}, "2025-04-30"
    )["eligible"]
    assert not q.assess_source({"kind": "search_snippet", "raw": {}}, "2025-04-30")[
        "eligible"
    ]
    controlled = q.assess_source(
        {"kind": "attachment_original", "raw": {}}, "2025-04-30"
    )
    assert controlled["eligible"] and controlled["historical_availability"] == "unknown"
    assert (
        q.parse_research_as_of("2025-04-30").isoformat()
        == "2025-04-30T23:59:59.999999+08:00"
    )


def test_task_brief_parser_rejects_ambiguous_and_truncated_objects():
    q = quality()
    task = (
        '{"task_id":"one","role":"searcher","objective":"facts","as_of":"2025-04-30"}'
    )
    assert q.parse_task_brief("说明```json\n" + task + "\n```").task_id == "one"
    with pytest.raises(ValueError):
        q.parse_task_brief(task + task)
    with pytest.raises(ValueError):
        q.parse_task_brief(task[:-1])


def test_version_two_manifest_requires_semantics_but_old_manifest_is_readable():
    from research_copilot.sandbox.contracts import ResultManifest

    raw = {
        "schema_version": 1,
        "input_refs": [],
        "metrics": [
            {"key": "m", "value": "1", "unit": "pp", "input_keys": ["assumption"]}
        ],
        "charts": [],
        "assumptions": {
            "assumption": {"value": "1", "basis": "scenario", "impact": "estimate"}
        },
    }
    assert ResultManifest.model_validate(raw).schema_version == 1
    with pytest.raises(ValueError, match="semantic"):
        ResultManifest.model_validate({**raw, "schema_version": 2})
    raw["metrics"][0].update(
        semantic_type="rate_difference",
        formula_id="custom",
        numerator_basis="rates",
        denominator_basis="revenue",
        is_estimate=True,
        display_precision=2,
    )
    assert (
        ResultManifest.model_validate({**raw, "schema_version": 2}).schema_version == 2
    )


def test_relevance_requires_entity_and_topic_and_rounded_error_is_computed():
    from research_copilot.subagents import source_policy

    brief = {"entity_terms": ["五粮液", "000858"], "topic_terms": ["年报", "业绩"]}
    assert source_policy.qualify_text("五粮液2024年报业绩", brief)["relevant"]
    assert not source_policy.qualify_text("洋河股份产业融合", brief)["relevant"]
    assert quality().rounding_error("9.36", 2, "8.3272") == "0.0416360"


def test_fact_count_is_rejected_before_any_evidence_access():
    from types import SimpleNamespace
    from research_copilot.subagents.facts import validate_facts

    with pytest.raises(ValueError, match="max_facts"):
        validate_facts(
            SimpleNamespace(facts=[None] * 16), None, {"task_brief": {"max_facts": 15}}
        )


def test_required_conclusion_cannot_be_a_label_without_value_and_estimate_cannot_use_evidence_fallback():
    q = quality()
    metric = {
        "M": {
            "value": "1.65",
            "unit": "pp",
            "display_precision": 2,
            "is_estimate": True,
        }
    }
    conclusions = [{"claim_id": "gap", "metric_ids": ["M"], "required_in_report": True}]
    for body, mids, eids in [
        ("增长存在差异。", ["M"], []),
        ("增长相差1.65个百分点。", [], ["E"]),
    ]:
        doc = {
            "body_hash": hashlib.sha256(body.encode()).hexdigest(),
            "claims": [
                {
                    "start": 0,
                    "end": len(body),
                    "quote": body,
                    "metric_ids": mids,
                    "evidence_ids": eids,
                    "conclusion_id": "gap" if mids else None,
                }
            ],
        }
        assert q.validate_claims(
            body, doc, metric, {"E": {"raw": {"value": "1.65"}}}, conclusions
        )


def test_labeled_publication_date_in_source_body_is_eligible():
    q = quality()
    base = {
        "kind": "original_text",
        "source": {},
        "raw": {"qualification": {"relevant": True}},
    }
    eligible = {
        **base,
        "raw": {**base["raw"], "body": "公告日期:2025-03-15 宁德时代年度报告"},
    }
    assert q.assess_source(eligible, "2025-04-30")["eligible"]
    future = {
        **base,
        "raw": {**base["raw"], "body": "公告日期:2025-05-15 宁德时代年度报告"},
    }
    assert not q.assess_source(future, "2025-04-30")["eligible"]
    unlabeled = {**base, "raw": {**base["raw"], "body": "2025-03-15 宁德时代年度报告"}}
    assert not q.assess_source(unlabeled, "2025-04-30")["eligible"]
    chinese = {
        **base,
        "raw": {
            **base["raw"],
            "body": "正文" * 20000 + "内部控制评价报告全文披露日期 2025 年3 月15 日",
        },
    }
    assert q.assess_source(chinese, "2025-04-30")["eligible"]


def test_cited_sentence_requires_an_explicit_numeric_claim_binding():
    q = quality()
    body = "营业收入同比-9.70%，归母净利润同比+15.01%[^E-000002]。"
    manifest = {"body_hash": hashlib.sha256(body.encode()).hexdigest(), "claims": []}
    evidence = {
        "E-000002": {"raw": {"body": "营业收入同比-9.70%，归母净利润同比+15.01%"}}
    }
    assert q.validate_claims(body, manifest, {}, evidence, [])
    unsupported = {"E-000002": {"raw": {"body": "其他内容"}}}
    assert q.validate_claims(body, manifest, {}, unsupported, [])


def test_writer_requires_conversion_metric_and_accepts_signed_decrease_magnitudes():
    q = quality()
    body = (
        "营业收入同比减少38,904,491千元（约389.04亿元），归母净利率由11.01%升至14.02%。"
    )
    metrics = {
        "M-amount": {
            "value": "-38904491",
            "unit": "千元",
            "display_precision": 0,
            "is_estimate": False,
        },
        "M-margin2023": {
            "value": "11.01",
            "unit": "%",
            "display_precision": 2,
            "is_estimate": False,
        },
        "M-margin2024": {
            "value": "14.02",
            "unit": "%",
            "display_precision": 2,
            "is_estimate": False,
        },
    }
    conclusions = [
        {
            "claim_id": "C1",
            "text": "营业收入减少38,904,491千元（约389.04亿元），净利率由11.01%升至14.02%。",
            "metric_ids": ["M-amount", "M-margin2023", "M-margin2024"],
            "evidence_ids": [],
            "required_in_report": True,
        }
    ]
    manifest = {
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "claims": [
            {
                "quote": body,
                "start": 0,
                "end": len(body),
                "metric_ids": ["M-amount", "M-margin2023", "M-margin2024"],
                "evidence_ids": [],
                "conclusion_id": "C1",
            }
        ],
    }
    assert any(
        "389.04" in error
        for error in q.validate_claims(body, manifest, metrics, {}, conclusions)
    )
    metrics["M-converted"] = {
        "value": "389.04",
        "unit": "亿元",
        "display_precision": 2,
        "is_estimate": False,
    }
    manifest["claims"][0]["metric_ids"].append("M-converted")
    conclusions[0]["metric_ids"].append("M-converted")
    assert q.validate_claims(body, manifest, metrics, {}, conclusions) == []


def test_product_uncertainty_depends_on_input_precision():
    metric = {
        "key": "m",
        "value": "6",
        "unit": "CNY",
        "input_keys": ["a", "b"],
        "semantic_type": "inferred_amount",
        "formula_id": "product",
        "numerator_basis": "amount",
        "denominator_basis": "not_applicable",
        "is_estimate": True,
        "display_precision": 2,
    }
    coarse = quality().validate_metric_semantics(
        metric, {"a": {"raw_value": "3.0"}, "b": {"raw_value": "2.0"}}
    )
    precise = quality().validate_metric_semantics(
        metric, {"a": {"raw_value": "3.00"}, "b": {"raw_value": "2.00"}}
    )
    from decimal import Decimal

    assert Decimal(coarse["rounding_error"]) == Decimal("0.2525")
    assert Decimal(precise["rounding_error"]) == Decimal("0.025025")
