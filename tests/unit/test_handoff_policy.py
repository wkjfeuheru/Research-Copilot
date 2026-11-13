from types import SimpleNamespace
import pytest


def test_text_only_mcp_response_keeps_text_block_pointers():
    import json
    from research_copilot.subagents.facts import source_segments

    rows = [
        json.loads(line)
        for line in source_segments(
            {"structured": None, "text_blocks": ["Revenue 120"]}
        )
        .decode()
        .splitlines()
    ]
    assert any(
        row.get("pointer") == "/text_blocks/0" and row.get("text") == "Revenue 120"
        for row in rows
    )


def test_status_handoff_does_not_repeat_full_fact_sheet():
    from research_copilot.orchestration.tools import compact_status

    status = {
        "approved": False,
        "artifacts": {},
        "latest_role_results": {
            "searcher": {
                "summary": "collected",
                "status": "completed",
                "evidence_ids": ["E-1"],
                "facts": [{"quote": "long source " * 1000}],
                "source_warnings": ["secondary source"],
            }
        },
    }
    brief = compact_status(
        status, {"searcher": "/workspace/artifacts/d/fact_index.json"}
    )
    assert "facts" not in brief["latest_role_results"]["searcher"]
    assert brief["latest_role_results"]["searcher"]["fact_count"] == 1
    assert brief["latest_role_results"]["searcher"]["source_warnings"] == [
        "secondary source"
    ]
    assert "facts" in status["latest_role_results"]["searcher"]


@pytest.mark.asyncio
async def test_evidence_reads_and_grep_use_bounded_windows():
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch
    from research_copilot.contracts import Dispatch

    class Request(SimpleNamespace):
        def override(self, **kwargs):
            return Request(**{**vars(self), **kwargs})

    seen = []

    async def handler(request):
        seen.append(request.tool_call["args"])
        return "ok"

    token = active_dispatch.set(Dispatch("own", "analysist", "/workspace/tasks/own/"))
    try:
        mw = RoleFileScopeMiddleware("analysist")
        await mw.awrap_tool_call(
            Request(
                tool_call={
                    "name": "read_file",
                    "args": {
                        "file_path": "/workspace/evidence/own/E-1.segments.jsonl",
                        "limit": 1000,
                    },
                }
            ),
            handler,
        )
        await mw.awrap_tool_call(
            Request(
                tool_call={
                    "name": "grep",
                    "args": {"path": "/workspace/evidence/own", "pattern": "Revenue"},
                }
            ),
            handler,
        )
        assert seen[0]["limit"] == 80
        assert seen[1]["max_count"] == 8
    finally:
        active_dispatch.reset(token)
