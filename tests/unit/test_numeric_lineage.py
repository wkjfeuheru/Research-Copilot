import hashlib

import pytest

from research_copilot.subagents.claim_validation import validate_claims
from research_copilot.subagents.numeric_lineage import fact_supports


def manifest(body, **refs):
    return {
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "claims": [
            {"start": 0, "end": len(body), "quote": body, **refs},
        ],
    }


@pytest.mark.parametrize(
    "body, valid",
    [
        ("增长20%", True),
        ("growth 20 percent", True),
        ("增长20.01%", False),
        ("20 percentile", False),
        ("收入20元", False),
    ],
)
def test_metric_binding_accepts_equivalent_decimal_rendering_but_not_wrong_value_or_unit(
    body, valid
):
    errors = validate_claims(
        body,
        manifest(body, metric_ids=["M-growth"], evidence_ids=["E-1"]),
        {"M-growth": {"value": "20.00", "unit": "%", "display_precision": 2}},
        {"E-1": {}},
        [],
    )
    assert (not errors) is valid


@pytest.mark.parametrize(
    "body",
    ["增长20%（推算）", "增长20%（估算）"],
)
def test_estimate_marker_accepts_both_synonyms(body):
    errors = validate_claims(
        body,
        manifest(body, metric_ids=["M-growth"], evidence_ids=["E-1"]),
        {"M-growth": {"value": "20.00", "unit": "%", "display_precision": 2,
                      "is_estimate": True}},
        {"E-1": {}},
        [],
    )
    assert errors == []


def test_estimate_marker_error_names_the_marker_words():
    errors = validate_claims(
        "增长20%",
        manifest("增长20%", metric_ids=["M-growth"], evidence_ids=["E-1"]),
        {"M-growth": {"value": "20.00", "unit": "%", "display_precision": 2,
                      "is_estimate": True}},
        {"E-1": {}},
        [],
    )
    assert any("「推算」或「估算」" in error for error in errors)


@pytest.mark.parametrize("body", ["净利率2024%", "收入2024元", "1.00元，1.00%"])
def test_year_like_values_and_later_units_cannot_bypass_numeric_binding(body):
    assert validate_claims(
        body,
        manifest(body, metric_ids=["M-rate"], evidence_ids=["E-1"]),
        {"M-rate": {"value": "1", "unit": "%", "display_precision": 2}},
        {"E-1": {}},
        [],
    )


@pytest.mark.parametrize("with_span", [True, False])
def test_analysis_prose_and_footnote_cannot_authorize_fabricated_number(with_span):
    body = "2024年净利率999.00%[^E-000001]"
    conclusion = {
        "claim_id": "c",
        "text": body,
        "metric_ids": [],
        "evidence_ids": ["E-000001"],
    }
    claims = manifest(body, conclusion_id="c", evidence_ids=["E-000001"])
    if not with_span:
        claims["claims"] = []
    assert validate_claims(
        body,
        claims,
        {},
        {"E-000001": {"raw": {"body": "2024年收入100元"}}},
        [conclusion],
    )


def test_matching_number_in_unstructured_source_is_not_a_fact_binding():
    body = "2024年净利率100%[^E-000001]"
    assert validate_claims(
        body,
        manifest(body, evidence_ids=["E-000001"]),
        {},
        {"E-000001": {"raw": {"body": "2024年收入100元"}}},
        [],
    )


@pytest.mark.parametrize("change", [None, "period", "unit", "key", "value", "scale"])
def test_disclosed_number_requires_fact_field_period_unit_and_exact_value(change):
    body = "2024年收入100元[^E-000001]"
    binding = {
        "key": "revenue_2024",
        "evidence_id": "E-000001",
        "period": "2024",
        "unit": "CNY",
        "scale": "1",
    }
    fact = {
        "input_ref": {**binding, "raw_value": "100", "content_hash": "a" * 64},
        "quote": "2024年收入100元",
    }
    if change in {"period", "unit", "key", "scale"}:
        binding[change] = {
            "period": "2023",
            "unit": "%",
            "key": "profit",
            "scale": "10000",
        }[change]
    if change == "value":
        body = body.replace("100", "999")
    errors = validate_claims(
        body,
        manifest(body, evidence_ids=["E-000001"], fact_refs=[binding]),
        {},
        {"E-000001": {"content_hash": "a" * 64}},
        [],
        facts=[fact],
    )
    assert bool(errors) is (change is not None)


def _point_fact(period):
    # 合同负债类时点数：raw_value 带千分位，登记 period 可能是复合写法
    return {
        "raw_value": "11,689,880,975.04",
        "period": period,
        "unit": "元",
        "scale": "1",
    }


@pytest.mark.parametrize(
    "period, quote",
    [
        # 复合登记值 ↔ 正文简写
        ("2024年末/2024年12月31日", "2024年末合同负债11689880975.04元"),
        ("2024年末/2024年12月31日", "2024年12月31日合同负债11689880975.04元"),
        # 单一时点登记值 ↔ 等价简写
        ("2024年12月31日", "2024年末合同负债11689880975.04元"),
        ("2024年末", "2024年期末合同负债11689880975.04元"),
    ],
)
def test_point_in_time_period_matches_equivalent_body_writing(period, quote):
    fact = _point_fact(period)
    # end 为数字结束位置；其后的“元”用于单位匹配，与 numeric_spans 的 match.end() 一致
    end = quote.index("元")
    assert fact_supports(fact, "11689880975.04", quote, end)


@pytest.mark.parametrize(
    "period, quote",
    [
        # 年份不同不得匹配
        ("2023年末", "2024年末合同负债11689880975.04元"),
        # 非时点 period 仍按字面匹配，不变体放行
        ("2024年上半年", "2024年末合同负债11689880975.04元"),
    ],
)
def test_point_in_time_period_does_not_over_match(period, quote):
    fact = _point_fact(period)
    end = quote.index("元")
    assert not fact_supports(fact, "11689880975.04", quote, end)


def test_field_key_year_shorthand_errors_with_actionable_hint():
    """字段简写里的裸年份仍算未溯源数字，但错误要说明怎么改写。"""
    body = "核心业绩概览图来源revenue_2024/2023。"
    errors = validate_claims(
        body,
        manifest(body, evidence_ids=["E-1"]),
        {},
        {"E-1": {}},
        [],
    )
    assert any("疑似年份或字段标识简写" in error for error in errors)


def test_missing_fact_period_is_named_in_the_numeric_error():
    """只报数字不够：必须指出缺的是哪个事实期间，子代理才能一次修好。"""
    binding = {
        "key": "net_profit_deducted_2024",
        "evidence_id": "E-1",
        "period": "2024年度",
        "unit": "元",
        "scale": "1",
    }
    fact = {
        "input_ref": {**binding, "raw_value": "31,741,959,276.27", "content_hash": "a" * 64},
        "quote": "归属于上市公司股东的扣除非经常性损益的净利润 31,741,959,276.27",
    }
    body = "扣非归母净利润31741959276.27元"
    errors = validate_claims(
        body,
        manifest(body, evidence_ids=["E-1"], fact_refs=[binding]),
        {},
        {"E-1": {"content_hash": "a" * 64}},
        [],
        facts=[fact],
    )
    assert any("正文缺少事实期间「2024年度」" in error for error in errors)


def test_metric_precision_mismatch_points_to_display_precision():
    body = "销售收入100.004元"
    errors = validate_claims(
        body,
        manifest(body, metric_ids=["M-rev"], evidence_ids=["E-1"]),
        {"M-rev": {"value": "100.004", "unit": "元", "display_precision": 2}},
        {"E-1": {}},
        [],
    )
    assert any("display_precision=2" in error for error in errors)


def test_compound_fact_period_matches_leading_year_in_prose():
    """登记 period 带括号补充（2024年度（2024-01-01至2024-12-31））时按前导年度匹配。"""
    fact = {
        "raw_value": "89,175,178,322.70",
        "period": "2024年度（2024-01-01至2024-12-31）",
        "unit": "元",
        "scale": "1",
    }
    quote = "五粮液2024年度营业总收入89175178322.70元"
    end = quote.index("元")
    assert fact_supports(fact, "89175178322.70", quote, end)


def test_pct_unit_accepts_percent_sign_in_prose():
    """指标单位写成 pct 时，正文的 % 仍应视为同一单位。"""
    body = "净利率35.72%"
    errors = validate_claims(
        body,
        manifest(body, metric_ids=["M-net"], evidence_ids=["E-1"]),
        {"M-net": {"value": "35.72", "unit": "pct", "display_precision": 2}},
        {"E-1": {}},
        [],
    )
    assert errors == []


def test_evidence_identifier_is_not_a_numeric_claim():
    """正文引用 E-000001 是证据标识，不应被当成未溯源数字。"""
    body = "2024年度营业总收入891.75亿元（E-000001）"
    errors = validate_claims(
        body,
        manifest(body, evidence_ids=["E-1"]),
        {},
        {"E-1": {}},
        [],
    )
    # Evidence IDs may appear in the diagnostic context, but never as the
    # unsupported number itself.
    diagnoses = [error.split(" | context:", 1)[0] for error in errors]
    assert any("891.75" in error for error in diagnoses)
    assert not any("-000001" in error for error in diagnoses)
