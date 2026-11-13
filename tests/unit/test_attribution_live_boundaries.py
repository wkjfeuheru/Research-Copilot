"""Regression seams from the next real CATL attempt."""

from types import SimpleNamespace

import pytest

from research_copilot.contracts import Dispatch
from research_copilot.runtime.control import active_dispatch
from research_copilot.subagents.analysist import AnalysisService
from research_copilot.subagents.contracts import (
    BusinessInputError,
    MetricSpec,
    SearchResult,
)
from research_copilot.subagents.facts import validate_facts
from research_copilot.subagents.metric_quality import validate_metric_batch
from research_copilot.subagents.services import digest
from tests.unit.test_analysis_submission_repair import prepared
from tests.unit.test_attribution_precision import quarter_chain, comparison
from tests.unit.test_orchestration import context


def test_single_period_margins_can_be_compared_without_mixing_effects():
    inputs, metrics = quarter_chain()
    metrics[1]["comparison"] = comparison(base_period="2026Q3")
    metrics[3]["comparison"] = comparison(current_period="2026Q2")
    metrics[4]["comparison"] = comparison()
    checked = validate_metric_batch(metrics, inputs)
    assert checked["change"]["value"] == "-2.47"
    metrics[3]["comparison"]["denominator"] = "different"
    with pytest.raises(ValueError, match="comparison"):
        validate_metric_batch(metrics, inputs)


def test_null_reporting_scope_cannot_be_registered_as_consolidated():
    raw = {
        "items": [
            {
                "reporting_scope": None,
                "period_basis": "year_to_date",
                "lines": [{"value": "100", "currency": "CNY"}],
            }
        ]
    }
    evidence = {
        "raw": raw,
        "content_hash": digest(raw),
        "source": {"server": "test"},
        "collected_at": "2026-11-12",
        "kind": "financial_data",
    }
    state = SimpleNamespace(role_data={"evidence": {"E-000001": evidence}})
    result = SearchResult(
        task_id="s",
        status="completed",
        summary="found",
        evidence_ids=["E-000001"],
        facts=[
            {
                "key": "revenue",
                "evidence_id": "E-000001",
                "content_hash": digest(raw),
                "pointer": "/items/0/lines/0/value",
                "quote": "100",
                "raw_value": "100",
                "unit": "元",
                "currency": "CNY",
                "period": "2026H1",
                "entity": "CATL",
                "basis": "合并利润表",
            }
        ],
    )
    with pytest.raises(ValueError, match="reporting_scope"):
        validate_facts(result, state, {"evidence_ids": ["E-000001"]})
    fact = result.facts[0].model_copy(
        update={"basis": "利润表; reporting_scope=unknown; period_basis=year_to_date"}
    )
    fixed = result.model_copy(update={"facts": [fact]})
    assert validate_facts(fixed, state, {"evidence_ids": ["E-000001"]})


async def test_invalid_declarative_comparison_is_rejected_before_sandbox(tmp_path):
    repo, _, services, _, _, _ = await prepared(tmp_path)
    repo.state.role_data["fact_sheets"] = {
        "facts": {
            "evidence_ids": ["E-000001"],
            "facts": [
                {"input_ref": {"key": k, "raw_value": v, "evidence_id": "E-000001"}}
                for k, v in [("a", "2.00"), ("b", "1.00")]
            ],
        }
    }
    repo.state.role_data["scopes"]["original"]["fact_sheet_ids"] = ["facts"]

    class MustNotExecute:
        async def execute(self, *args):
            pytest.fail("invalid declaration consumed sandbox execution")

    spec = MetricSpec(
        key="mix_effect",
        formula_id="product",
        semantic_type="mix_effect",
        unit="pp",
        input_keys=["a", "b"],
        numerator_basis="effect",
        denominator_basis="revenue",
        display_precision=2,
    )
    token = active_dispatch.set(
        Dispatch("original", "analysist", "/workspace/tasks/original/")
    )
    try:
        with pytest.raises(BusinessInputError, match="comparison"):
            await AnalysisService(
                services, execution=MustNotExecute()
            ).run_metric_specs(context(), "invalid-specs", [spec], [])
    finally:
        active_dispatch.reset(token)
    assert len(repo.state.role_data["executions"]) == 1


def test_text_offsets_do_not_bypass_source_metadata_validation():
    from research_copilot.runtime.control import RunControl
    from research_copilot.sandbox.receipts import ExecutionService
    from research_copilot.sandbox.contracts import ResultManifest

    raw = {
        "items": [
            {"reporting_scope": None, "lines": [{"value": "100", "currency": None}]}
        ]
    }
    manifest = ResultManifest(
        schema_version=2,
        metrics=[],
        charts=[],
        input_refs=[
            {
                "key": "x",
                "evidence_id": "E-000001",
                "content_hash": digest(raw),
                "pointer": "/items/0/lines/0/value",
                "raw_value": "100",
                "unit": "元",
                "currency": "CNY",
                "period": "2026H1",
                "entity": "CATL",
                "basis": "合并利润表",
                "text_start": 0,
                "text_end": 3,
                "extraction": "text-extraction",
            }
        ],
    )
    state = SimpleNamespace(
        role_data={
            "evidence": {"E-000001": {"raw": raw, "content_hash": digest(raw)}},
            "scopes": {"d": {"evidence_ids": ["E-000001"]}},
        }
    )
    service = ExecutionService(None, control=RunControl(), budget=None)
    with pytest.raises(ValueError, match="currency|reporting_scope"):
        service._validate_inputs(
            manifest,
            state,
            SimpleNamespace(dispatch_id="d"),
            {"evidence_hashes": {"E-000001": digest(raw)}},
        )
