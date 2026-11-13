"""Evidence projection for the read-only workbench.

Historical sources and FinanceKB records share the same reader; knowledge
snippets must stay locatable (collection/page/section) without pretending to
be original documents.
"""

from types import SimpleNamespace

from research_copilot.web.workspace import evidence_body, evidence_records


def _state(**records):
    return SimpleNamespace(role_data={"evidence": records}, artifacts={})


def test_structured_financekb_evidence_is_readable():
    state = _state(
        **{
            "E-000001": {
                "parent_id": "T-1",
                "kind": "financial_data",
                "content_hash": "a" * 64,
                "collected_at": "2026-01-02T03:04:05+00:00",
                "raw": {
                    "title": "年度报告",
                    "structured": {
                        "available": True,
                        "domain": "fundamentals",
                        "items": [{"report_date": "2025-12-31"}],
                    },
                },
                "source": {"server": "financekb_fundamentals"},
            }
        }
    )

    record = evidence_records(state)[0]
    assert record["source"] == "financekb_fundamentals"
    assert record["published_at"] is None
    assert record["collection"] == ""
    assert record["page"] is None

    summary, body = evidence_body(state, "E-000001")
    assert summary["evidence_id"] == "E-000001"
    assert '"report_date": "2025-12-31"' in body


def test_knowledge_snippet_keeps_locator_not_fulltext_claim():
    state = _state(
        **{
            "E-000002": {
                "parent_id": None,
                "kind": "search_snippet",
                "content_hash": "b" * 64,
                "collected_at": "2026-01-03T00:00:00+00:00",
                "raw": {
                    "title": "某券商深度报告",
                    "body": "片段正文",
                    "source_ref": {"collection": "research"},
                    "structured": {
                        "page": 12,
                        "heading_path": "第二章/盈利预测",
                        "broker": "示例证券",
                        "report_type": "company",
                        "date": "2025-11-30",
                    },
                },
                "source": {"server": "financekb_knowledge"},
            }
        }
    )

    record = evidence_records(state)[0]
    assert record["content_kind"] == "search_snippet"
    assert record["collection"] == "research"
    assert record["page"] == 12
    assert record["heading_path"] == "第二章/盈利预测"
    assert record["broker"] == "示例证券"
    assert record["report_type"] == "company"
    assert record["published_at"] == "2025-11-30"

    _, body = evidence_body(state, "E-000002")
    assert body == "片段正文"
