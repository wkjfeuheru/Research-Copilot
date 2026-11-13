import json
from types import SimpleNamespace

import pytest

from research_copilot.runtime.business import ControlState
from research_copilot.subagents.contracts import BusinessInputError
from research_copilot.subagents.services import digest


def test_writer_context_contains_copy_ready_facts_from_confirmed_analysis():
    from research_copilot.contracts import ArtifactRef
    from research_copilot.subagents.source_context import writing_context
    from research_copilot.workspace import bundled_resources

    fact = dict(
        key="cost_h1",
        raw_value="210654890000",
        unit="元",
        scale="1",
        period="2026H1（2026-06-30，累计）",
        evidence_id="E-1",
        currency="unknown",
        basis="reporting_scope=unknown",
        entity="CATL",
    )
    ref = ArtifactRef(artifact_id="a", version="1", content_hash="a" * 64)
    state = ControlState(
        artifacts={"analysis": ref},
        role_data={
            "objects": {
                "a": {
                    "kind": "analysis",
                    "dispatch_id": "d",
                    "payload": {
                        "evidence_ids": ["E-1"],
                        "verified_facts": [{"input_ref": fact}],
                    },
                }
            },
        },
    )
    result = writing_context(
        state,
        {"evidence_ids": ["E-1"]},
        "w",
        resources_root=bundled_resources(),
        report_type="company_report",
    )
    assert result["analysis"]["verified_facts"] == [fact]


def test_short_authorized_source_is_inlined_with_raw_pointer_not_projection_path():
    from research_copilot.subagents.source_context import source_context

    evidence = {
        "E-1": {"raw": {"body": "Revenue 100"}, "content_hash": "a" * 64},
        "E-2": {"raw": {"body": "not authorized"}, "content_hash": "b" * 64},
    }
    state = ControlState(role_data={"evidence": evidence})
    payload = source_context(state, {"evidence_ids": ["E-1"]})
    assert payload[0]["source_document"] == {"body": "Revenue 100"}
    assert payload[0]["pointers"] == ["/body"]
    assert "not authorized" not in json.dumps(payload)


def test_large_source_is_not_inlined_or_silently_truncated():
    from research_copilot.subagents.source_context import source_context

    state = ControlState(
        role_data={
            "evidence": {
                "E-1": {"raw": {"body": "长" * 10000}, "content_hash": "a" * 64}
            }
        }
    )
    payload = source_context(state, {"evidence_ids": ["E-1"]})
    assert "source_document" not in payload[0]
    assert payload[0]["read_required"] is True


def test_invalid_projection_pointer_error_contains_exact_correctable_location():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "Revenue 100"}
    fact = SimpleNamespace(
        key="revenue", evidence_id="E-1", content_hash=digest(raw), pointer="/raw/body"
    )
    result = SimpleNamespace(facts=[fact], evidence_ids=["E-1"])
    state = ControlState(
        role_data={"evidence": {"E-1": {"raw": raw, "content_hash": digest(raw)}}}
    )
    with pytest.raises(BusinessInputError) as error:
        validate_facts(result, state, {"evidence_ids": ["E-1"]})
    assert "revenue" in str(error.value) and "/body" in str(error.value)
    assert "source-root" in str(error.value)


def test_quote_repair_identifies_field_and_rejects_transformed_source_value():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "Sales exceeded 200GWh"}
    fact = SimpleNamespace(
        key="sales_volume",
        evidence_id="E-1",
        content_hash=digest(raw),
        pointer="/body",
        quote="Sales exceeded 200GWh",
        raw_value=">200",
        currency="不适用",
        basis="原文披露",
    )
    fact.model_dump = lambda **kwargs: {}
    result = SimpleNamespace(facts=[fact], evidence_ids=["E-1"])
    state = ControlState(
        role_data={"evidence": {"E-1": {"raw": raw, "content_hash": digest(raw)}}}
    )
    with pytest.raises(BusinessInputError) as caught:
        validate_facts(result, state, {"evidence_ids": ["E-1"]})
    assert all(
        part in str(caught.value) for part in ("sales_volume", "E-1", "/body", ">200")
    )


def test_analysis_context_points_to_original_fact_sheet_instead_of_retyping_refs():
    from research_copilot.subagents.source_context import analysis_context

    ref = {
        "key": "revenue",
        "raw_value": "100",
        "unit": "CNY",
        "period": "2024",
        "basis": "consolidated",
        "content_hash": "a" * 64,
    }
    state = ControlState(
        role_data={
            "fact_sheets": {
                "facts-1": {"facts": [{"input_ref": ref}], "evidence_ids": ["E-1"]}
            }
        }
    )
    result = analysis_context(
        state, {"fact_sheet_ids": ["facts-1"], "evidence_ids": ["E-1"]}, "d"
    )
    assert result["fact_sheet_paths"] == ["/workspace/artifacts/d/facts-1.json"]
    assert result["facts"][0]["key"] == "revenue"
    assert "content_hash" not in result["facts"][0]
    assert "input_ref" in result["loader_example"]


def test_wuliangye_price_range_is_defined_as_channel_discount_not_expense_sensitivity():
    from pathlib import Path

    criteria = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/wuliangye_r4/acceptance_criteria.json"
        ).read_text("utf-8")
    )
    definition = criteria["metric_definitions"]["p5_price_discount"]
    assert (
        definition["formula"]
        == "(ex_factory_price - wholesale_price) / ex_factory_price * 100"
    )
    assert definition["input_fields"] == [
        "p5_ex_factory_price",
        "p5_wholesale_price_low",
        "p5_wholesale_price_high",
    ]


def test_analysis_handoff_inlines_only_authorized_small_original_sources():
    from research_copilot.subagents.source_context import analysis_context

    state = ControlState(
        role_data={
            "evidence": {
                "E-1": {
                    "raw": {"body": "Ex-factory 1019; wholesale 910-920"},
                    "content_hash": "a" * 64,
                },
                "E-private": {"raw": {"body": "secret"}, "content_hash": "b" * 64},
            }
        }
    )
    payload = analysis_context(state, {"evidence_ids": ["E-1"]}, "new-dispatch")
    assert payload["sources"][0]["source_document"]["body"].startswith("Ex-factory")
    assert payload["sources"][0]["pointers"] == ["/body"]
    assert "secret" not in json.dumps(payload)
    assert "untrusted" in payload["source_usage"]


def test_analysis_handoff_keeps_large_source_as_index():
    from research_copilot.subagents.source_context import analysis_context

    state = ControlState(
        role_data={
            "evidence": {
                "E-1": {"raw": {"body": "x" * 10000}, "content_hash": "a" * 64}
            }
        }
    )
    payload = analysis_context(state, {"evidence_ids": ["E-1"]}, "d")
    assert payload["sources"][0]["read_required"] is True
    assert "source_document" not in payload["sources"][0]


def _fact(raw, **overrides):
    from research_copilot.subagents.contracts import EvidenceFact

    values = dict(
        key="revenue_2024",
        evidence_id="E-1",
        content_hash=digest(raw),
        pointer="/body",
        quote="",
        raw_value="362,012,554",
        unit="千元",
        currency="CNY",
        period="2024",
        entity="宁德时代",
        basis="合并",
    )
    values.update(overrides)
    return EvidenceFact(**values)


def _evidence_state(raw):
    return ControlState(
        role_data={
            "evidence": {
                "E-1": {
                    "raw": raw,
                    "content_hash": digest(raw),
                    "source": {"server": "web_search"},
                    "collected_at": "2026-01-01",
                    "kind": "original_text",
                }
            }
        }
    )


def test_malformed_quote_is_derived_from_a_unique_raw_value():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "2024年营业收入 362,012,554 千元，上年同期 400,917,045 千元。"}
    fact = _fact(raw, quote="营业收入同比下降")
    result = SimpleNamespace(
        facts=[fact],
        evidence_ids=["E-1"],
        status="partial",
        stop_reason="source_exhausted",
    )

    facts = validate_facts(result, _evidence_state(raw), {"evidence_ids": ["E-1"]})

    assert facts[0]["quote"].count("362,012,554") == 1
    ref = facts[0]["input_ref"]
    assert raw["body"][ref["text_start"] : ref["text_end"]] == "362,012,554"


def test_ambiguous_raw_value_is_rejected_with_copy_ready_source_windows():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "营业收入 362,012,554 千元。母公司营业收入 362,012,554 千元。"}
    fact = _fact(raw, quote="营业收入")
    result = SimpleNamespace(
        facts=[fact],
        evidence_ids=["E-1"],
        status="partial",
        stop_reason="source_exhausted",
    )

    with pytest.raises(BusinessInputError) as caught:
        validate_facts(result, _evidence_state(raw), {"evidence_ids": ["E-1"]})

    message = str(caught.value)
    assert "more than once" in message
    assert "营业收入 362,012,554 千元" in message


def test_undeclared_or_outside_fact_evidence_is_repairable():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "Revenue 120"}
    fact = _fact(raw)
    result = SimpleNamespace(
        facts=[fact], evidence_ids=[], status="partial", stop_reason="source_exhausted"
    )
    with pytest.raises(BusinessInputError, match="evidence_ids"):
        validate_facts(result, _evidence_state(raw), {"evidence_ids": ["E-1"]})
    result.evidence_ids = ["E-1"]
    result.facts = [_fact(raw, evidence_id="E-2")]
    with pytest.raises(BusinessInputError, match="outside this dispatch"):
        validate_facts(result, _evidence_state(raw), {"evidence_ids": ["E-1"]})


def test_repeated_row_quote_is_accepted_with_first_occurrence_offsets():
    from research_copilot.subagents.facts import validate_facts

    row = "营业收入（千元） | 362,012,554 | 400,917,045 | -9.70%"
    raw = {"body": f"主要会计数据 {row}\n分季度数据 {row}\n"}
    fact = _fact(raw, quote=row)
    result = SimpleNamespace(
        facts=[fact],
        evidence_ids=["E-1"],
        status="partial",
        stop_reason="source_exhausted",
    )

    facts = validate_facts(result, _evidence_state(raw), {"evidence_ids": ["E-1"]})

    ref = facts[0]["input_ref"]
    assert raw["body"][ref["text_start"] : ref["text_end"]] == "362,012,554"
    assert ref["text_start"] == raw["body"].index("362,012,554")


def test_all_invalid_facts_are_reported_in_one_repair_message():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "营业收入 100 千元，净利润 50 千元。"}
    facts = [
        _fact(raw, key="alpha", quote="不存在的引文"),
        _fact(raw, key="beta", quote="净利润 50 千元", raw_value="50"),
        _fact(raw, key="gamma", quote="成本 99"),
    ]
    result = SimpleNamespace(
        facts=facts,
        evidence_ids=["E-1"],
        status="partial",
        stop_reason="source_exhausted",
    )

    with pytest.raises(BusinessInputError) as caught:
        validate_facts(result, _evidence_state(raw), {"evidence_ids": ["E-1"]})

    message = str(caught.value)
    assert "alpha" in message and "gamma" in message
    assert "beta" not in message


def test_structured_wrapup_keeps_values_and_period_metadata_for_each_quarter():
    from research_copilot.subagents.source_context import source_context
    from research_copilot.subagents.facts import document

    items = []
    expected = {}
    for index, (date, revenue, cost) in enumerate(
        [
            ("2026-09-30", "442137424000", "341695013000"),
            ("2026-06-30", "276916580000", "210654890000"),
            ("2026-03-31", "129131041000", "97086370000"),
        ]
    ):
        lines = [
            {
                "item_id": "OPERATE_INCOME",
                "name": "营业收入",
                "value": revenue,
                "unit": "元",
                "currency": "CNY",
            },
            {
                "item_id": "OPERATE_COST",
                "name": "营业成本",
                "value": cost,
                "unit": "元",
                "currency": "CNY",
            },
        ] + [
            {
                "item_id": f"OTHER_{i}",
                "name": f"其他科目{i}",
                "value": str(i),
                "currency": None,
            }
            for i in range(45)
        ]
        items.append(
            {
                "period_date": date,
                "period_basis": "year_to_date",
                "reporting_scope": None,
                "symbol": "300750.SZ",
                "lines": lines,
            }
        )
        expected.update(
            {
                f"/items/{index}/lines/0/value": (revenue, date),
                f"/items/{index}/lines/1/value": (cost, date),
            }
        )
    raw = {"structured": {"items": items}}
    original = digest(raw)
    state = ControlState(
        role_data={
            "evidence": {
                "E-1": {"raw": raw, "content_hash": original, "kind": "financial_data"},
                "private": {
                    "raw": {"body": "private-secret"},
                    "content_hash": "b" * 64,
                },
            }
        }
    )
    binding = {
        "evidence_ids": ["E-1"],
        "task_brief": {
            "objective": "登记各期营业收入、营业成本原值，不计算派生值",
            "required_fields": [
                "revenue_2026_ytd_q3",
                "cost_2026_ytd_q3",
                "revenue_2026_ytd_h1",
                "cost_2026_ytd_h1",
                "revenue_2026_ytd_q1",
                "cost_2026_ytd_q1",
            ],
            "topic_terms": ["累计营业收入", "累计营业成本"],
        },
    }

    rows = source_context(state, binding, wrap_up=True)
    spans = {span["pointer"]: span for row in rows for span in row.get("excerpts", [])}
    assert set(expected) <= set(spans), (
        "Wrap-up must expose values, not just matching item IDs"
    )
    for pointer, (number, date) in expected.items():
        span = spans[pointer]
        assert span["text"] == number
        assert span["context"]["period_date"] == date
        assert span["context"]["period_basis"] == "year_to_date"
        assert span["context"]["reporting_scope"] is None
        assert span["record"]["currency"] == "CNY"
        value = document(raw)
        for part in pointer[1:].split("/"):
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert value[span["start"] : span["end"]] == span["text"]
    assert digest(raw) == original
    assert "private-secret" not in json.dumps(rows)
    assert len(json.dumps(rows, ensure_ascii=False).encode()) <= 8192
