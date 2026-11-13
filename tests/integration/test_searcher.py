"""Searcher business integration: real graph/store/files, scripted external boundaries."""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from research_copilot.contracts import Dispatch
from research_copilot.runtime.control import RunControl
from tests.unit.test_orchestration import ScriptModel, call, config, context


async def dispatch(env, role="searcher", ident="collect-1", description="collect"):
    services, business, _ = env
    scope = Dispatch(ident, role, f"/workspace/tasks/{ident}/")
    await business.authorize_dispatch(context(), scope)
    if not description.lstrip().startswith("{"):
        state = await services.repository.read_control(context())
        description = json.dumps(
            {
                "task_id": {
                    "searcher": "collect",
                    "analysist": "analysis",
                    "writer": "write",
                    "reviewer": "review",
                }[role],
                "role": role,
                "objective": description,
                "as_of": state.role_data.get("research_as_of", "2025-04-30"),
            }
        )
    await services.prepare_dispatch(context(), scope, description)
    return scope


@pytest.mark.asyncio
async def test_frozen_task_remains_readable_after_conversation_compaction(role_env):
    services, _, _ = role_env
    scope = await dispatch(
        role_env,
        description="Compute A-H exactly; retain this task after summarization.",
    )
    path = f"/workspace/artifacts/{scope.dispatch_id}/task.md"
    await services.validate_material_path(context(), scope, path)
    target = services.report_root(context()) / path.removeprefix("/workspace/")
    assert (
        json.loads(target.read_text("utf-8"))["objective"]
        == "Compute A-H exactly; retain this task after summarization."
    )
    target.write_text("changed task", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        await services.validate_material_path(context(), scope, path)


@pytest.mark.asyncio
async def test_new_dispatch_can_list_its_empty_material_directories(role_env):
    services, _, _ = role_env
    scope = await dispatch(role_env)
    for kind in ("evidence", "artifacts"):
        path = f"/workspace/{kind}/{scope.dispatch_id}"
        await services.validate_material_path(context(), scope, path)
        assert (services.report_root(context()) / kind / scope.dispatch_id).is_dir()
    with pytest.raises(PermissionError):
        await services.validate_material_path(
            context(), scope, f"/workspace/artifacts/{scope.dispatch_id}/invented.json"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_receipt,repair_succeeds",
    [
        (False, True),
        (True, True),
        (True, False),
        ("schema", True),
        (True, "source_loop"),
    ],
)
async def test_searcher_native_task_registers_evidence_and_maps_structured_result(
    role_env, tmp_path, invalid_receipt, repair_succeeds
):
    from research_copilot.orchestration import RoleBinding, build_supervisor
    from research_copilot.subagents.searcher import build_search_tools, SourceTool
    from research_copilot.subagents.contracts import SearchResult
    from research_copilot.workspace import bundled_resources

    services, business, store = role_env

    async def fetch(args):
        return {
            "title": "Annual filing",
            "body": "Revenue 120",
            "published_at": "2025-03-01",
        }

    source = SourceTool(
        "financekb_revenue",
        "financekb_market",
        "actual_tool",
        "Read filing",
        {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
        fetch,
    )
    tools = build_search_tools(services, [source])
    role = ScriptModel(
        script=[
            call("financekb_revenue", {"symbol": "AAA"}, "fetch-1"),
            call(
                "SearchResult",
                {
                    "task_id": "collect",
                    "status": "completed",
                    "summary": "x" * 1201
                    if invalid_receipt == "schema"
                    else "filing acquired",
                    "evidence_ids": ["E-000001"],
                    "coverage": ["revenue"],
                    "artifact_refs": (
                        [
                            {
                                "artifact_id": "input-plan",
                                "version": "1",
                                "content_hash": "a" * 64,
                            }
                        ]
                        if invalid_receipt is True
                        else []
                    ),
                    "gaps": [],
                    "source_warnings": [],
                },
                "result-1",
            ),
            call(
                "SearchResult",
                {
                    "task_id": "collect",
                    "status": "completed",
                    "summary": "filing acquired",
                    "evidence_ids": ["E-000001"],
                    "coverage": ["revenue"],
                    "artifact_refs": [],
                    "gaps": [],
                    "source_warnings": [],
                },
                "repair-1",
            ),
        ]
    )
    if not repair_succeeds:
        role.script[-1] = role.script[1]
    if repair_succeeds == "source_loop":
        role.script[-1] = call("financekb_revenue", {"symbol": "AAA"}, "repeat-source")
    parent = ScriptModel(
        script=[
            call(
                "task",
                {
                    "subagent_type": "searcher",
                    "description": '{"task_id":"collect","role":"searcher","objective":"collect","as_of":"2025-04-30"}',
                },
            ),
            AIMessage(content="done"),
        ]
    )
    bindings = {
        name: RoleBinding(
            ScriptModel(script=[AIMessage(content="unused")]), [], implemented=False
        )
        for name in ("searcher", "analysist", "writer")
    }
    bindings["searcher"] = RoleBinding(role, tools, response_schema=SearchResult)
    agent = build_supervisor(
        model=parent,
        roles=bindings,
        review_graph=__import__(
            "tests.unit.test_orchestration", fromlist=["review_graph"]
        ).review_graph(),
        report_id="report-1",
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
        checkpointer=InMemorySaver(),
        business=business,
        control=RunControl(),
        budget=await store.load_budget(context()),
    )
    if not repair_succeeds or repair_succeeds == "source_loop":
        # Repair exhaustion surfaces as a supervisor-visible task error; the
        # acquisition ledger is preserved and nothing was accepted.
        result = await agent.ainvoke(
            {"messages": [("user", "collect")]}, config(), context=context()
        )
        message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        payload = json.loads(message.content)
        assert message.name == "task"
        assert payload["error"] == "role_result_repair_exhausted"
        assert role.position == 4  # one acquisition + initial result + two repairs
        assert len((await store.read_control(context())).role_data["acquisitions"]) == 1
        return
    result = await agent.ainvoke(
        {"messages": [("user", "collect")]}, config(), context=context()
    )
    output = json.loads(
        next(m.content for m in result["messages"] if isinstance(m, ToolMessage))
    )
    if invalid_receipt:
        message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert message.status == "success"
        assert output["status"] == "completed"
        state = await store.read_control(context())
        assert len(state.role_data["acquisitions"]) == 1
        assert role.position == 3
        assert parent.position == 2
        if invalid_receipt is True:
            rejected = next(iter(state.role_data["rejected_task_results"].values()))
            assert rejected["result"]["artifact_refs"][0]["artifact_id"] == "input-plan"
        assert state.role_data["evidence"]["E-000001"]
        assert result["messages"][-1].content == "done"
        return
    assert output["status"] == "completed"
    assert output["references"][0]["artifact_id"] == "E-000001"
    assert (await business.get_status(context()))["latest_role_results"]["searcher"][
        "coverage"
    ] == ["revenue"]
    state = await store.read_control(context())
    evidence = state.role_data["evidence"]["E-000001"]
    assert evidence["raw"]["body"] == "Revenue 120"
    assert evidence["source"]["server"] == "financekb_market"
    assert evidence["source"]["historical_availability"] == "unknown"
    assert "get_evidence" not in {t.name for t in role.tools_seen}
    assert "register_evidence" not in {t.name for t in role.tools_seen}
    source_message = next(
        m
        for m in role.messages_seen
        if isinstance(m, ToolMessage) and m.name == "financekb_revenue"
    )
    assert json.loads(source_message.content)["data"]["evidence_id"] == "E-000001"
    assert any(
        "/workspace/evidence/" in str(m.content) and "/index.json" in str(m.content)
        for m in role.messages_seen
    )


@pytest.mark.asyncio
async def test_fetch_registration_replay_does_not_repeat_source_and_checks_projection_hash(
    role_env,
):
    services, _, store = role_env
    scope = await dispatch(role_env)
    calls = []

    async def fetch(args):
        calls.append(args)
        return {"body": "verified"}

    from research_copilot.runtime.control import active_dispatch

    token = active_dispatch.set(scope)
    try:
        first = await services.acquire(
            context(), "fetch-1", "financekb_market", "read", {}, fetch
        )
        again = await services.acquire(
            context(), "fetch-1", "financekb_market", "read", {}, fetch
        )
        assert first == again
        assert len(calls) == 1
        path = services.report_root(context()) / first["path"].removeprefix(
            "/workspace/"
        )
        path.write_text("tampered", encoding="utf-8")
        with pytest.raises(ValueError, match="hash"):
            await services.validate_material_path(context(), scope, first["path"])
        with pytest.raises(PermissionError):
            await services.validate_material_path(
                context(), scope, "/workspace/evidence/other/index.json"
            )
        assert len((await store.read_control(context())).role_data["evidence"]) == 1
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_failed_source_query_is_reused_and_raw_registration_can_resume(
    role_env, monkeypatch
):
    from research_copilot.subagents.searcher import SourceUnavailable
    from research_copilot.runtime.control import active_dispatch

    services, _, store = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)
    calls = []

    async def failed(args):
        calls.append(args)
        raise SourceUnavailable("credential-must-not-leak")

    try:
        for call_id in ("first", "second"):
            output = await services.acquire(
                context(), call_id, "financekb_market", "read", {"symbol": "AAA"}, failed
            )
            assert not output["ok"]
            assert "credential-must-not-leak" not in json.dumps(output)
        assert len(calls) == 1
        assert not (await store.read_control(context())).role_data.get("evidence")

        async def success(args):
            calls.append(args)
            return {"body": "raw result preserved"}

        original = services.project

        async def unavailable(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(services, "project", unavailable)
        with pytest.raises(OSError):
            await services.acquire(
                context(), "recover", "financekb_market", "read", {}, success
            )
        monkeypatch.setattr(services, "project", original)
        receipt = await services.acquire(
            context(), "recover", "financekb_market", "read", {}, success
        )
        assert receipt["evidence_id"] == "E-000001"
        assert len(calls) == 2
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_source_failure_is_structured_and_cached_per_query(role_env):
    """同一查询失败后不再重复取数，且异常文本不进入模型可见回执。"""
    from research_copilot.runtime.control import active_dispatch
    from research_copilot.subagents.searcher import SourceUnavailable

    services, _, store = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)
    calls = []

    async def failing(args):
        calls.append(args)
        raise SourceUnavailable("credential-must-not-leak", code="tool_error")

    try:
        for call_id in ("first", "second"):
            output = await services.acquire(
                context(),
                call_id,
                "financekb_market",
                "resolve_symbol",
                {"symbol": "600519.SH"},
                failing,
            )
            assert not output["ok"]
            assert output["error"]["code"] == "tool_error"
            assert "credential-must-not-leak" not in json.dumps(output)
        assert len(calls) == 1
        failures = (await store.read_control(context())).role_data["source_failures"]
        assert len(failures) == 1
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_identical_values_for_different_source_requests_keep_distinct_lineage(
    role_env,
):
    from research_copilot.runtime.control import active_dispatch

    services, _, _ = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)

    async def fetch(args):
        return {"value": "120"}

    try:
        one = await services.acquire(
            context(), "one", "financekb_market", "read", {"symbol": "AAA"}, fetch
        )
        two = await services.acquire(
            context(), "two", "financekb_market", "read", {"symbol": "BBB"}, fetch
        )
        assert one["evidence_id"] != two["evidence_id"]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_database_failure_after_fetch_recovers_raw_without_refetch(
    role_env, monkeypatch
):
    from research_copilot.runtime.control import active_dispatch

    services, _, store = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)
    calls = []
    original = store.mutate_control
    fail_once = True

    async def failing(context, operation):
        nonlocal fail_once
        if calls and fail_once:
            fail_once = False
            raise OSError("database disconnected")
        return await original(context, operation)

    async def fetch(args):
        calls.append(args)
        return {"body": "original response"}

    monkeypatch.setattr(store, "mutate_control", failing)
    try:
        with pytest.raises(OSError):
            await services.acquire(
                context(), "replay", "financekb_market", "read", {}, fetch
            )
        receipt = await services.acquire(
            context(), "replay", "financekb_market", "read", {}, fetch
        )
        assert receipt["ok"]
        assert len(calls) == 1
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_cancelled_source_completion_is_unknown_and_never_automatically_repeated(
    role_env,
):
    import asyncio
    from research_copilot.runtime.control import active_dispatch

    services, _, _ = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)
    calls = []

    async def fetch(args):
        calls.append(args)
        raise asyncio.CancelledError()

    try:
        with pytest.raises(asyncio.CancelledError):
            await services.acquire(
                context(), "unknown", "financekb_market", "read", {}, fetch
            )
        with pytest.raises(RuntimeError, match="unknown"):
            await services.acquire(
                context(), "unknown", "financekb_market", "read", {}, fetch
            )
        assert len(calls) == 1
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_mcp_cancellation_propagates_instead_of_returning_source_failure(
    role_env,
):
    from research_copilot.sources.mcp_client import MCPCancelled
    from research_copilot.runtime.control import active_dispatch

    services, _, _ = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)

    async def fetch(args):
        raise MCPCancelled("cancelled")

    try:
        with pytest.raises(MCPCancelled):
            await services.acquire(
                context(), "cancelled", "financekb_market", "read", {}, fetch
            )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_evidence_projection_is_paginated_and_searchable_without_changing_original(
    role_env,
):
    from research_copilot.runtime.control import active_dispatch
    from research_copilot.workspace.filesystem import ReportFilesystemBackend

    services, _, store = role_env
    scope = await dispatch(role_env)
    token = active_dispatch.set(scope)
    text = "a" * 15000 + "UNIQUE-END"

    async def fetch(args):
        return {"body": text}

    try:
        receipt = await services.acquire(
            context(), "long", "financekb_market", "read", {}, fetch
        )
        relative = receipt["path"].removeprefix("/workspace")
        backend = ReportFilesystemBackend(root_dir=services.report_root(context()))
        page = backend.read(relative, offset=0, limit=5)
        assert page.next_offset == 5
        assert page.total_lines > 50
        assert backend.grep("UNIQUE-END", relative).matches[0]["line"] > 5
        state = await store.read_control(context())
        assert (
            state.role_data["evidence"][receipt["evidence_id"]]["raw"]["body"] == text
        )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_dispatch_brief_is_bound_to_the_approved_plan_task(role_env):
    from research_copilot.subagents.contracts import BusinessInputError

    # task_id matches the plan's frozen 'collect' brief; a bound field deviates.
    with pytest.raises(BusinessInputError, match="deviates"):
        await dispatch(
            role_env,
            description=json.dumps(
                {
                    "task_id": "collect",
                    "role": "searcher",
                    "objective": "collect",
                    "as_of": "2025-04-30",
                    "max_facts": 99,
                }
            ),
        )
    # Free-form wording and per-dispatch search guidance may differ; bound
    # fields match, so the dispatch is accepted against the frozen contract.
    scope = await dispatch(
        role_env,
        ident="bound-ok",
        description=json.dumps(
            {
                "task_id": "collect",
                "role": "searcher",
                "objective": "公司事实采集（细化措辞）",
                "as_of": "2025-04-30",
                "entity_terms": ["五粮液"],
            }
        ),
    )
    assert scope.write_prefix.endswith("bound-ok/")


@pytest.mark.asyncio
async def test_partial_searcher_result_with_all_required_facts_maps_to_completed(
    role_env,
):
    from research_copilot.subagents.contracts import EvidenceFact, Gap, SearchResult
    from research_copilot.subagents.services import digest

    services, _, store = role_env
    scope = await dispatch(role_env)
    raw = {"title": "Annual report", "body": "Revenue 120"}
    content_hash = digest(raw)

    def seed(state):
        state.role_data.setdefault("evidence", {})["E-000001"] = {
            "kind": "original_text",
            "raw": raw,
            "content_hash": content_hash,
            "source": {"server": "test", "tool": "fetch", "arguments": {}},
            "collected_at": "2025-04-30T00:00:00+00:00",
        }
        binding = state.role_data["scopes"][scope.dispatch_id]
        binding["evidence_ids"] = ["E-000001"]
        binding["task_brief"]["required_fields"] = ["revenue_2024"]
        binding["task_brief"]["optional_fields"] = ["gross_margin_2024"]

    await store.mutate_control(context(), seed)
    result = SearchResult(
        task_id="collect",
        status="partial",
        summary="required revenue collected; optional margin missing",
        evidence_ids=["E-000001"],
        facts=[
            EvidenceFact(
                key="revenue_2024",
                evidence_id="E-000001",
                content_hash=content_hash,
                pointer="/body",
                quote="Revenue 120",
                raw_value="120",
                unit="元",
                currency="CNY",
                period="2024",
                entity="AAA",
                basis="合并",
            )
        ],
        gaps=[
            Gap(
                description="gross margin missing",
                impact="report must state the limitation",
                suggested_role="searcher",
            )
        ],
        stop_reason="required_complete",
    )
    parsed = await services.map_result(
        context(), scope, result.model_dump(), validate_only=True
    )
    assert parsed.status == "completed"
    assert parsed.gaps


@pytest.mark.asyncio
async def test_missing_derived_margin_does_not_block_downstream(role_env):
    from research_copilot.subagents.contracts import EvidenceFact, Gap, SearchResult
    from research_copilot.subagents.services import digest

    services, _, store = role_env
    scope = await dispatch(role_env)
    raw = {"title": "Annual report", "body": "Revenue 120 Cost 80"}
    content_hash = digest(raw)

    def seed(state):
        state.role_data.setdefault("evidence", {})["E-000001"] = {
            "kind": "original_text",
            "raw": raw,
            "content_hash": content_hash,
            "source": {"server": "test", "tool": "fetch", "arguments": {}},
            "collected_at": "2025-04-30T00:00:00+00:00",
        }
        binding = state.role_data["scopes"][scope.dispatch_id]
        binding["evidence_ids"] = ["E-000001"]
        binding["task_brief"]["required_fields"] = [
            "revenue_2024",
            "operating_cost_2024",
            "gross_margin_2024",
        ]

    await store.mutate_control(context(), seed)

    def fact(key, quote, value):
        return EvidenceFact(
            key=key,
            evidence_id="E-000001",
            content_hash=content_hash,
            pointer="/body",
            quote=quote,
            raw_value=value,
            unit="元",
            currency="CNY",
            period="2024",
            entity="AAA",
            basis="合并",
        )

    result = SearchResult(
        task_id="collect",
        status="partial",
        summary="revenue and cost collected; the margin is computed by analysis",
        evidence_ids=["E-000001"],
        facts=[
            fact("revenue_2024", "Revenue 120", "120"),
            fact("operating_cost_2024", "Cost 80", "80"),
        ],
        gaps=[
            Gap(
                description="gross margin is computed from revenue and operating cost",
                impact="report states the computation basis",
                suggested_role="analysist",
            )
        ],
        stop_reason="source_exhausted",
    )
    parsed = await services.map_result(
        context(), scope, result.model_dump(), validate_only=True
    )
    assert parsed.status == "completed"


@pytest.mark.asyncio
async def test_knowledge_acquisition_registers_snippets_and_reuses_completed_page(
    role_env,
):
    from functools import partial

    from research_copilot.runtime.control import active_dispatch
    from research_copilot.subagents.searcher import _knowledge_normalizer

    services, _, store = role_env
    scope = await dispatch(
        role_env,
        description=json.dumps(
            {
                "task_id": "collect",
                "role": "searcher",
                "objective": "collect channel evidence",
                "as_of": "2025-04-30",
                "entity_terms": ["贵州茅台"],
                "topic_terms": ["渠道"],
            }
        ),
    )
    token = active_dispatch.set(scope)
    items = [
        {"id": "doc-1", "chunk": 1, "title": "贵州茅台渠道跟踪", "content": "渠道库存下降。", "date": "2025-03-01", "score": 0.9},
        {"id": "doc-2", "chunk": 4, "title": "贵州茅台批价跟踪", "content": "渠道批价保持稳定。", "date": "2025-04-01", "score": 0.8},
    ]
    calls = []

    async def fetch(args):
        calls.append(dict(args))
        return {
            "structured": {
                "collection": "research",
                "as_of": "2025-04-30",
                "page": 1,
                "page_size": 10,
                "returned": len(items),
                "has_more": False,
                "next_page": None,
                "items": items,
            },
            "text_blocks": [],
            "unsupported_blocks": [],
        }

    normalizer = partial(_knowledge_normalizer, tool_name="search_research")
    query = {"query": "贵州茅台 渠道 2025"}
    try:
        first = await services.acquire(
            context(),
            "knowledge-1",
            "financekb_knowledge",
            "search_research",
            query,
            fetch,
            normalize=normalizer,
        )
        replay = await services.acquire(
            context(),
            "knowledge-2",
            "financekb_knowledge",
            "search_research",
            query,
            fetch,
            normalize=normalizer,
        )
        assert len(calls) == 1
        assert [row["evidence_id"] for row in replay["evidence"]] == [
            row["evidence_id"] for row in first["evidence"]
        ]
        assert len(first["evidence"]) == 2
        assert first["pagination"]["page"] == 1
        record = (await store.read_control(context())).role_data["evidence"][
            first["evidence"][0]["evidence_id"]
        ]
        assert record["kind"] == "search_snippet"
        assert record["source"]["provenance"]["response_hash"] == first["response_hash"]
        assert record["source"]["provenance"]["item_pointer"] == "/items/0"
        assert record["raw"]["published_at"] == "2025-03-01"

        async def empty(args):
            return {
                "structured": {
                    "collection": "research",
                    "as_of": "2025-04-30",
                    "page": 1,
                    "page_size": 10,
                    "returned": 0,
                    "has_more": False,
                    "next_page": None,
                    "items": [],
                },
                "text_blocks": [],
                "unsupported_blocks": [],
            }

        empty_page = await services.acquire(
            context(),
            "knowledge-empty",
            "financekb_knowledge",
            "search_research",
            {"query": "无匹配主题"},
            empty,
            normalize=normalizer,
        )
        assert empty_page["evidence"] == [] and empty_page["warnings"]
        state = await store.read_control(context())
        assert len(state.role_data["evidence"]) == 2
    finally:
        active_dispatch.reset(token)
