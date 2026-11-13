import pytest

from research_copilot.subagents.claim_validation import numeric_spans
from research_copilot.subagents.numeric_lineage import fact_supports


@pytest.mark.parametrize(
    "text",
    [
        "2026Q3累计-2026H1累计，变化-2.4653pp",
        "2026Q2/Q3比较；2026-06-30累计，变化-2.4653pp",
        "2026H1，2026Q1累计，变化-2.4653pp",
        "2026年1-9月累计，变化-2.4653pp",
        "2026年9M累计，变化-2.4653pp",
        "2026Q1累计/2026H1累计/2026 9M累计，变化-2.4653pp",
    ],
)
def test_quarter_labels_are_not_misread_as_financial_values(text):
    assert [m.group() for m in numeric_spans(text)] == ["-2.4653"]


@pytest.mark.parametrize(
    "text", ["2026H1毛利率2026%", "2026Q3利润-2026元", "Q3.5成本100元"]
)
def test_calendar_labels_do_not_hide_unregistered_amounts(text):
    assert any(
        m.group() in {"2026", "-2026", "3.5", "100"} for m in numeric_spans(text)
    )


@pytest.mark.parametrize(
    "unit", ["pp", "pct_point", "percentage_point", "percentage points"]
)
@pytest.mark.parametrize(
    "display, valid", [("个百分点", True), ("pp", True), ("%", False)]
)
def test_percentage_point_units_render_without_becoming_percent(unit, display, valid):
    from research_copilot.subagents.claim_validation import validate_claims
    from tests.unit.test_numeric_lineage import manifest

    text = f"毛利率环比下降2.47{display}"
    errors = validate_claims(
        text,
        manifest(text, metric_ids=["delta"], evidence_ids=["E-1"]),
        {"delta": {"value": "-2.47", "unit": unit, "display_precision": 2}},
        {"E-1": {}},
        [],
    )
    assert (not errors) is valid


@pytest.mark.parametrize(
    "text,valid",
    [
        ("环比2.47个百分点的降幅也随之失真", True),
        ("环比降幅为2.47个百分点", True),
        ("环比2.47pp的降幅", True),
        ("环比2.47个百分点的增幅", False),
        ("环比上升2.47个百分点", False),
        ("环比2.47个百分点", False),
    ],
)
def test_negative_metric_magnitude_requires_local_decrease_meaning(text, valid):
    from research_copilot.subagents.claim_validation import validate_claims
    from tests.unit.test_numeric_lineage import manifest

    errors = validate_claims(
        text,
        manifest(text, metric_ids=["delta"], evidence_ids=["E-1"], conclusion_id="c"),
        {"delta": {"value": "-2.47", "unit": "pct_point", "display_precision": 2}},
        {"E-1": {}},
        [
            {
                "claim_id": "c",
                "metric_ids": ["delta"],
                "evidence_ids": ["E-1"],
                "required_in_report": True,
            }
        ],
    )
    assert (not errors) is valid
    if not valid:
        assert any(text in error for error in errors), errors


@pytest.mark.parametrize(
    "label, valid",
    [
        ("2026Q3累计", True),
        ("2026年前三季度", True),
        ("2026年1-9月累计", True),
        ("2026年9M累计", True),
        ("2026 9M累计", True),
        ("2026年6M累计", False),
        ("2026Q3单季", False),
        ("2026Q2累计", False),
        ("2025Q3累计", False),
    ],
)
def test_cumulative_period_aliases_cannot_turn_into_single_quarter(label, valid):
    fact = {
        "raw_value": "442137424000",
        "period": "2026-09-30 (2026Q3累计/YTD)",
        "unit": "元",
        "scale": "1",
    }
    quote = label + "营业收入442137424000元"
    assert fact_supports(fact, fact["raw_value"], quote, quote.index("元")) is valid
