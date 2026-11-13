"""Regressions minimized from the second real Wuliangye run."""

import pytest

from research_copilot.subagents.claim_validation import validate_claims
from research_copilot.workspace.filesystem import ReportFilesystemBackend
from tests.unit.test_numeric_lineage import manifest


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_single_file_grep_does_not_search_sibling_evidence(
    tmp_path, monkeypatch, asynchronous
):
    backend = ReportFilesystemBackend(root_dir=tmp_path)
    # Exercise the fallback used on this Windows installation, independent of rg.
    monkeypatch.setattr(backend, "_ripgrep_search", lambda *a, **kw: (None, False))
    (tmp_path / "other.json").write_text("风险：错误来源\n", encoding="utf-8")
    (tmp_path / "report.jsonl").write_text(
        "年报\n风险：正确来源\n结束\n", encoding="utf-8"
    )
    if asynchronous:
        result = await backend.agrep("风险", "/report.jsonl", max_count=1)
    else:
        result = backend.grep("风险", "/report.jsonl", max_count=1)
    assert result.error is None
    assert result.matches == [
        {"path": "/report.jsonl", "line": 2, "text": "风险：正确来源"}
    ]
    assert not result.truncated
    assert backend.grep("错误来源", "/report.jsonl").matches == []


@pytest.mark.parametrize(
    "period_text,valid",
    [
        ("2024年", True),
        ("2024年度", True),
        ("2023年", False),
        ("2024年一季度", False),
        ("2024年末", False),
        ("2024年3月", False),
    ],
)
def test_annual_fact_accepts_year_wording_but_not_different_period(period_text, valid):
    binding = {
        "key": "revenue_2024",
        "evidence_id": "E-1",
        "period": "2024年度",
        "unit": "元",
        "scale": "1",
    }
    fact = {
        "input_ref": {**binding, "raw_value": "89,175,178,322.70", "content_hash": "a"}
    }
    body = f"{period_text}收入89,175,178,322.70元"
    errors = validate_claims(
        body,
        manifest(body, evidence_ids=["E-1"], fact_refs=[binding]),
        {},
        {"E-1": {"content_hash": "a"}},
        [],
        facts=[fact],
    )
    assert (not errors) is valid


@pytest.mark.parametrize(
    "key,valid", [("ocf_to_net_profit_2024", True), ("unregistered_2024", False)]
)
def test_registered_metric_identifier_is_not_a_numeric_claim(key, valid):
    body = f"覆盖倍数1.07倍（指标{key}）"
    errors = validate_claims(
        body,
        manifest(body, metric_ids=["M-1"]),
        {
            "M-1": {
                "value": "1.07",
                "unit": "ratio",
                "local_key": "ocf_to_net_profit_2024",
            }
        },
        {},
        [],
    )
    assert (not errors) is valid


def test_single_file_grep_preserves_cap_glob_and_context(tmp_path):
    backend = ReportFilesystemBackend(root_dir=tmp_path)
    (tmp_path / "report.jsonl").write_text(
        "前文\n风险一\n中间\n风险二\n", encoding="utf-8"
    )
    result = backend.grep(
        "风险", "/report.jsonl", "*.jsonl", max_count=1, context_lines=1
    )
    assert result.truncated
    assert result.matches == [
        {
            "path": "/report.jsonl",
            "line": 2,
            "text": "风险一",
            "context_before": [{"line": 1, "text": "前文"}],
            "context_after": [{"line": 3, "text": "中间"}],
        }
    ]
    assert backend.grep("风险", "/report.jsonl", "*.md").matches == []
    assert len(backend.grep("风险", "/report.jsonl", "/**/*.jsonl").matches) == 2
    assert backend.grep("风险", "/missing.jsonl").matches == []


@pytest.mark.parametrize(
    "body",
    [
        "覆盖2024倍（指标ocf_to_net_profit_2024）",
        "覆盖1.07倍（指标ocf_to_net_profit_2024_extra）",
    ],
)
def test_metric_key_exemption_cannot_authorize_other_numbers(body):
    errors = validate_claims(
        body,
        manifest(body, metric_ids=["M-1"]),
        {
            "M-1": {
                "value": "1.07",
                "unit": "ratio",
                "local_key": "ocf_to_net_profit_2024",
            }
        },
        {},
        [],
    )
    assert errors


@pytest.mark.asyncio
@pytest.mark.parametrize("include_value", [False, True])
async def test_writer_requires_frozen_fields_in_visible_claims(include_value):
    from types import SimpleNamespace

    from research_copilot.subagents.services import digest
    from research_copilot.subagents.writer import WritingService

    binding = {
        "key": "revenue_2024",
        "evidence_id": "E-000001",
        "period": "2024年度",
        "unit": "元",
        "scale": "1",
    }
    raw = {"body": "2024年度收入100元"}
    evidence = {"raw": raw, "content_hash": digest(raw)}
    fact = {"input_ref": {**binding, "raw_value": "100", "content_hash": digest(raw)}}
    prose = "2024年度收入100元" if include_value else "原值见指标附录"
    body = "## 结论\n\n" + prose + "[^E-000001]"
    state = SimpleNamespace(
        role_data={
            "workflow_version": "products-v2",
            "metrics": {"a": {}},
            "evidence": {"E-000001": evidence},
            "scopes": {
                "w": {
                    "evidence_ids": ["E-000001"],
                    "task_brief": {
                        "required_fields": ["revenue_2024"],
                        "optional_fields": ["profit_2024"],
                    },
                }
            },
        }
    )
    # A valid reference alone is insufficient: the registered value must be rendered.
    result = await WritingService(None)._check_body(
        None,
        dispatch=SimpleNamespace(dispatch_id="w"),
        state=state,
        analysis={
            "dispatch_id": "a",
            "payload": {
                "metric_ids": [],
                "evidence_ids": ["E-000001"],
                "verified_facts": [fact],
                "chart_ids": [],
            },
        },
        template="## 结论",
        body=body,
        claim_manifest=manifest(body, evidence_ids=["E-000001"], fact_refs=[binding]),
        report_type="flash_comment",
        analysis_ids=["a"],
    )
    assert result["ok"] is include_value
    if not include_value:
        assert "required fact not rendered: revenue_2024" in result["errors"]
