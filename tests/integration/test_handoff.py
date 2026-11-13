import json
import pytest
from research_copilot.runtime.control import active_dispatch
from tests.integration.test_searcher import dispatch
from tests.unit.test_orchestration import context


async def collected(role_env, *, text_only_mcp=False):
    services, _, _ = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)

    async def fetch(args):
        body = "2024 Revenue 120 CNY. 2023 Revenue 100 CNY."
        return (
            {"structured": None, "text_blocks": [body]}
            if text_only_mcp
            else {"body": body}
        )

    try:
        receipt = await services.acquire(
            context(), "source", "sina_finance", "read", {}, fetch
        )
    finally:
        active_dispatch.reset(token)
    return scope, receipt


def result(receipt):
    return {
        "task_id": "collect",
        "status": "completed",
        "summary": "Two-year revenue collected",
        "evidence_ids": [receipt["evidence_id"]],
        "facts": [
            {
                "key": "revenue_2024",
                "evidence_id": receipt["evidence_id"],
                "content_hash": receipt["content_hash"],
                "pointer": "/body",
                "quote": "2024 Revenue 120 CNY.",
                "raw_value": "120",
                "unit": "CNY",
                "currency": "CNY",
                "period": "2024",
                "entity": "Example",
                "basis": "consolidated",
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("text_only_mcp", [False, True])
async def test_fact_sheet_handoff_preserves_original_locator_and_scoped_projection(
    role_env, text_only_mcp
):
    services, _, store = role_env
    scope, receipt = await collected(role_env, text_only_mcp=text_only_mcp)
    value = result(receipt)
    if text_only_mcp:
        value["facts"][0]["pointer"] = "/text_blocks/0"
    await services.map_result(context(), scope, value)
    later = await dispatch(role_env, "analysist", "analysis")
    root = services.report_root(context())
    index_path = f"/workspace/artifacts/{later.dispatch_id}/fact_index.json"
    await services.validate_material_path(context(), later, index_path)
    index = json.loads(
        (root / index_path.removeprefix("/workspace/")).read_text("utf-8")
    )
    sheet = json.loads(
        (root / index[0]["path"].removeprefix("/workspace/")).read_text("utf-8")
    )
    item = sheet["facts"][0]
    assert item["input_ref"]["raw_value"] == "120"
    assert item["input_ref"]["text_start"] == 13
    assert item["input_ref"]["extraction"] == "text-extraction"
    assert item["quote"] == "2024 Revenue 120 CNY."
    from research_copilot.sandbox.contracts import ResultManifest
    from research_copilot.sandbox.receipts import ExecutionService

    manifest = ResultManifest(
        schema_version=1, input_refs=[item["input_ref"]], metrics=[], charts=[]
    )
    state = await store.read_control(context())
    verified = ExecutionService._validate_inputs(
        None,
        manifest,
        state,
        later,
        {"evidence_hashes": {receipt["evidence_id"]: receipt["content_hash"]}},
    )
    assert verified["revenue_2024"]["raw_value"] == "120"
    assert len((await store.read_control(context())).role_data["evidence"]) == 1
    with pytest.raises(PermissionError):
        await services.validate_material_path(
            context(), later, index[0]["path"].replace("/analysis/", "/collect-1/")
        )


@pytest.mark.asyncio
async def test_fact_sheet_rejects_invented_or_ambiguous_quote(role_env):
    from research_copilot.subagents.contracts import BusinessInputError

    services, _, store = role_env
    scope, receipt = await collected(role_env)
    value = result(receipt)
    value["facts"][0]["quote"] = "invented 120"
    with pytest.raises(BusinessInputError, match="quote"):
        await services.map_result(context(), scope, value)
    assert not (await store.read_control(context())).role_data.get("fact_sheets")


@pytest.mark.asyncio
async def test_fact_sheet_hash_mismatch_stays_fatal(role_env):
    services, _, _ = role_env
    scope, receipt = await collected(role_env)
    value = result(receipt)
    value["facts"][0]["content_hash"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        await services.map_result(context(), scope, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_number", ["12", "20"])
async def test_fact_cannot_extract_only_part_of_a_number(role_env, partial_number):
    from research_copilot.subagents.contracts import BusinessInputError

    services, _, _ = role_env
    scope, receipt = await collected(role_env)
    value = result(receipt)
    value["facts"][0]["raw_value"] = partial_number
    with pytest.raises(BusinessInputError, match="numeric|quote"):
        await services.map_result(context(), scope, value)


@pytest.mark.asyncio
async def test_precise_source_projection_contains_raw_pointer_and_offsets(role_env):
    services, _, _ = role_env
    scope, receipt = await collected(role_env)
    path = f"/workspace/evidence/{scope.dispatch_id}/{receipt['evidence_id']}.segments.jsonl"
    await services.validate_material_path(context(), scope, path)
    rows = [
        json.loads(line)
        for line in (services.report_root(context()) / path.removeprefix("/workspace/"))
        .read_text("utf-8")
        .splitlines()
    ]
    assert rows[0]["pointer"] == "/body"
    assert rows[0]["start"] == 0
    assert rows[0]["text"].startswith("2024 Revenue")


@pytest.mark.asyncio
async def test_required_fields_cannot_be_silently_completed(role_env):
    from research_copilot.subagents.contracts import TaskBrief, BusinessInputError

    services, _, _ = role_env
    brief = TaskBrief(
        task_id="required",
        role="searcher",
        objective="collect",
        as_of="2025-04-30",
        required_fields=["revenue_2024"],
        optional_fields=["cfo_2024"],
    )
    from tests.integration.support import approve_task

    await approve_task(role_env, brief)
    scope = await dispatch(
        role_env, ident="required", description=brief.model_dump_json()
    )
    with pytest.raises(BusinessInputError, match="required"):
        await services.map_result(
            context(),
            scope,
            {
                "task_id": "required",
                "status": "completed",
                "summary": "done",
                "stop_reason": "required_complete",
            },
        )


@pytest.mark.asyncio
async def test_optional_gap_does_not_force_more_collection(role_env):
    from research_copilot.subagents.contracts import TaskBrief

    services, _, store = role_env
    _, receipt = await collected(role_env)
    brief = TaskBrief(
        task_id="required",
        role="searcher",
        objective="collect",
        as_of="2025-04-30",
        required_fields=["revenue_2024"],
        optional_fields=["cfo_2024"],
    )
    from tests.integration.support import approve_task

    await approve_task(role_env, brief)
    scope = await dispatch(
        role_env, ident="required", description=brief.model_dump_json()
    )
    value = {
        **result(receipt),
        "task_id": "required",
        "stop_reason": "required_complete",
    }
    mapped = await services.map_result(context(), scope, value)
    assert mapped.status == "completed"
    sheet = next(
        iter((await store.read_control(context())).role_data["fact_sheets"].values())
    )
    assert sheet["missing_required"] == []
    assert sheet["missing_optional"] == ["cfo_2024"]


@pytest.mark.asyncio
async def test_collection_no_progress_stops_without_another_source_call(role_env):
    from types import SimpleNamespace
    from research_copilot.subagents.middleware import RoleBusinessMiddleware
    from research_copilot.runtime.control import RunControl
    from langchain_core.messages import ToolMessage

    services, _, _ = role_env
    scope = await dispatch(role_env)
    mw = RoleBusinessMiddleware("searcher", services, RunControl(), ["web_search"])
    calls = []

    async def handler(request):
        calls.append(request.tool_call["id"])
        return ToolMessage(
            content=json.dumps({"ok": True, "items": []}),
            tool_call_id=request.tool_call["id"],
        )

    token = active_dispatch.set(scope)
    try:
        for i in range(3):
            req = SimpleNamespace(
                tool_call={
                    "id": str(i),
                    "name": "web_search",
                    "args": {"query": str(i)},
                },
                runtime=SimpleNamespace(context=context()),
                state={"messages": []},
            )
            response = await mw.awrap_tool_call(req, handler)
        assert len(calls) == 2
        assert "collection_stop" in response.content
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_result_repair_does_not_repeat_acquisition(role_env):
    from types import SimpleNamespace
    from research_copilot.subagents.middleware import (
        RoleBusinessMiddleware,
        REPAIR_MARKER,
    )
    from research_copilot.runtime.control import RunControl
    from langchain_core.messages import ToolMessage

    services, _, _ = role_env
    scope = await dispatch(role_env)
    mw = RoleBusinessMiddleware("searcher", services, RunControl(), ["web_search"])
    req = SimpleNamespace(
        tool_call={"id": "new", "name": "web_search", "args": {}},
        state={
            "messages": [
                ToolMessage(content=REPAIR_MARKER + "bad schema", tool_call_id="old")
            ]
        },
    )

    async def handler(request):
        pytest.fail("Repair must not recollect")

    token = active_dispatch.set(scope)
    try:
        assert "result_repair_only" in (await mw.awrap_tool_call(req, handler)).content
    finally:
        active_dispatch.reset(token)
