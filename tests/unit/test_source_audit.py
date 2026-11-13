import json
from contextlib import nullcontext

import pytest

from research_copilot.runtime.source_audit import audit_context, span
from tests.unit.test_orchestration import context


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_source_span_exports_correlated_metadata_without_payload(failed):
    events = []

    async def export(event):
        events.append(event)

    expectation = pytest.raises(RuntimeError) if failed else nullcontext()
    with audit_context(context(), export):
        with expectation:
            async with span("source", "mcp.search_news", source="mcp") as row:
                row["provider_request_id"] = "provider-123"
                row["secret"] = "response-secret"
                row["payload"] = {"query": "private-query"}
                if failed:
                    raise RuntimeError("key-secret")
    assert len(events) == 1
    row = events[0]
    assert row["run_id"] == "run-1" and row["report_id"] == "report-1"
    assert row["status"] == ("failed" if failed else "completed")
    assert row["duration_ms"] >= 0
    assert row["span_id"]
    assert row["provider_request_id"] == "provider-123"
    assert set(row) <= {
        "span_id",
        "category",
        "name",
        "source",
        "status",
        "dispatch_id",
        "report_id",
        "run_id",
        "budget_id",
        "duration_ms",
        "error_type",
        "provider_request_id",
    }
    assert not any(
        secret in json.dumps(events)
        for secret in ("key-secret", "response-secret", "private-query")
    )
