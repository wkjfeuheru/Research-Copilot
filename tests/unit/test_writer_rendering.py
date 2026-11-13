import pytest

from research_copilot.subagents.writer_rendering import render_sections


def inputs():
    fact = dict(
        key="sales",
        evidence_id="E-1",
        period="2026Q3累计",
        unit="元",
        scale="1",
        raw_value="120",
        content_hash="x",
    )
    return {
        "verified_facts": [{"input_ref": fact}],
        "claims": [
            {
                "claim_id": "C1",
                "text": "毛利率下降2.47个百分点",
                "metric_ids": ["M-1"],
                "evidence_ids": ["E-1"],
                "required_in_report": True,
            }
        ],
        "metric_ids": ["M-1"],
        "evidence_ids": ["E-1"],
    }, {"M-1": {"value": "-2.47", "unit": "pct_point", "display_precision": 2}}


def test_explicit_tokens_bind_title_facts_and_required_conclusions():
    payload, metrics = inputs()
    title, sections = render_sections(
        "变化{{metric:M-1}}",
        ["收入{{fact:sales}}", "{{conclusion:C1}}"],
        ["1. 数据", "2. 结论"],
        payload,
        metrics,
    )
    assert "-2.47个百分点" in title
    assert sections[0].heading == "1. 数据"
    assert "2026Q3累计：120元" in sections[0].text
    assert sections[0].claims[0].fact_refs[0].key == "sales"
    assert any(c.quote == title and c.metric_ids == ["M-1"] for c in sections[0].claims)
    assert sections[1].claims[0].conclusion_id == "C1"


@pytest.mark.parametrize(
    "texts",
    [
        ["{{fact:unknown}}", "{{conclusion:C1}}"],
        ["收入120元", "遗漏结论"],
        ["{{conclusion:C1}}"],
    ],
)
def test_unknown_references_missing_conclusions_and_wrong_section_count_fail(texts):
    payload, metrics = inputs()
    with pytest.raises(ValueError):
        render_sections("标题", texts, ["1. 数据", "2. 结论"], payload, metrics)


@pytest.mark.parametrize(
    "period",
    [
        "2026-01-01~2026-09-30 (YTD, period_date 2026-09-30)",
        "2026-01-01/2026-06-30",
        "2026-06-30",
    ],
)
def test_rendered_period_never_merges_with_amount_and_passes_fact_audit(period):
    from research_copilot.subagents.writer import WritingService
    from research_copilot.subagents.claim_validation import validate_claims

    payload, metrics = inputs()
    payload["verified_facts"][0]["input_ref"]["period"] = period
    title, sections = render_sections(
        "标题",
        ["{{fact:sales}}", "{{conclusion:C1}}"],
        ["数据", "结论"],
        payload,
        metrics,
    )
    body = "\n".join(s.text for s in sections)
    manifest = WritingService(None)._build_claim_manifest(body, sections, metrics)
    assert not validate_claims(
        body,
        manifest,
        metrics,
        {"E-1": {"content_hash": "x"}},
        payload["claims"],
        facts=payload["verified_facts"],
        required_fields=["sales"],
    )
