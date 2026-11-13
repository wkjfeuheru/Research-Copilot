import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from research_copilot.runtime.business import ControlState
from research_copilot.runtime.control import RunControl
from research_copilot.subagents.middleware import RoleBusinessMiddleware


def environment(binding):
    state = ControlState(
        role_data={
            "scopes": {"d": binding},
            "workflow_version": "products-v2",
            "report_scope": {"report_type": "flash_comment"},
        },
        dispatches={"d": {"inputs": {}}},
        plans=[{"plan": {"report_type": "flash_comment"}}],
    )
    mapped = []

    class Services:
        repository = None

        async def scope(self, context, role):
            return SimpleNamespace(
                dispatch_id="d", write_prefix="/workspace/tasks/d/"
            ), state

        async def mutate_control(self, context, operation):
            return operation(state)

        async def map_result(self, context, scope, payload, validate_only=False):
            mapped.append(payload)

    services = Services()
    services.repository = services
    services.mapped = mapped

    class Request(SimpleNamespace):
        def override(self, **kwargs):
            return Request(**(vars(self) | kwargs))

    request = Request(
        runtime=SimpleNamespace(context=None),
        messages=[],
        state={"messages": []},
        tools=[{"name": "web_search"}, {"name": "read_file"}],
        system_message=None,
    )
    return (
        RoleBusinessMiddleware("searcher", services, RunControl(), ["web_search"]),
        request,
        state,
    )


@pytest.mark.asyncio
async def test_shared_source_exhaustion_still_allows_reading_registered_originals():
    from research_copilot.subagents.collection import begin_source_call

    mw, req, state = environment(
        {"evidence_ids": [], "task_brief": {"task_id": "repair", "max_source_calls": 1}}
    )
    state.dispatches["d"]["work_group"] = "same-scope"
    ledger = {
        "calls": {"prior:1": "hash"},
        "max_source_calls": 1,
        "no_progress_calls": 0,
        "max_no_progress_calls": 2,
    }
    state.role_data["collection_work"] = {"same-scope": ledger}

    async def capture(request):
        assert request.tools == [{"name": "read_file"}]
        assert "已登记" in request.system_message.content

    await mw.awrap_model_call(req, capture)
    scope, _ = await mw.services.scope(None, "searcher")
    assert (
        await begin_source_call(
            mw.services, None, scope, {"id": "new", "name": "web_search", "args": {}}
        )
        == "source_call_limit"
    )
    assert len(ledger["calls"]) == 1


@pytest.mark.asyncio
async def test_truncated_analysis_submission_gets_one_retry():
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from research_copilot.runtime.control import RunControl
    from research_copilot.subagents.middleware import RoleBusinessMiddleware

    class Services:
        async def scope(self, context, role):
            return SimpleNamespace(
                dispatch_id="d", write_prefix="/workspace/tasks/d/"
            ), SimpleNamespace(role_data={"scopes": {"d": {}}})

    middleware = RoleBusinessMiddleware("analysist", Services(), RunControl())
    state = {
        "messages": [
            AIMessage(
                content="writing the candidate",
                invalid_tool_calls=[
                    {
                        "name": "write_file",
                        "args": '{"candidate":',
                        "id": "x",
                        "error": None,
                    }
                ],
                response_metadata={"finish_reason": "length"},
            )
        ]
    }
    runtime = SimpleNamespace(context=None)
    update = await middleware.aafter_model(state, runtime)
    assert update["retry_truncated_analysis"] is True
    assert update["jump_to"] == "model"
    # 重试前剥离 invalid_tool_calls，避免下一轮被补丁成孤儿 ToolMessage。
    assert update["messages"][0].invalid_tool_calls == []
    assert update["messages"][0].id == state["messages"][0].id
    state["retry_truncated_analysis"] = True
    assert await middleware.aafter_model(state, runtime) is None


@pytest.mark.asyncio
async def test_flash_comment_writer_cannot_use_file_fallback():
    from types import SimpleNamespace

    from langchain_core.messages import ToolMessage

    from research_copilot.runtime.control import RunControl
    from research_copilot.subagents.middleware import RoleBusinessMiddleware

    state = SimpleNamespace(
        plans=[{"plan": {"report_type": "flash_comment"}}],
        role_data={
            "scopes": {"d": {"skill_reads": []}},
            "workflow_version": "products-v2",
            "report_scope": {"report_type": "flash_comment"},
        },
        dispatches={"d": {"inputs": {}}},
    )

    class Services:
        async def scope(self, context, role):
            return SimpleNamespace(
                dispatch_id="d", write_prefix="/workspace/tasks/d/"
            ), state

    async def handler(request):
        pytest.fail("file fallback must not execute for flash_comment")

    middleware = RoleBusinessMiddleware(
        "writer", Services(), RunControl(), ["validate_draft", "submit_draft"]
    )
    request = SimpleNamespace(
        tool_call={"id": "1", "name": "validate_draft", "args": {}},
        runtime=SimpleNamespace(context=None),
        state={"messages": []},
    )
    response = await middleware.awrap_tool_call(request, handler)
    assert isinstance(response, ToolMessage)
    assert "structured_submission_required" in response.content


@pytest.mark.asyncio
async def test_repeated_evidence_history_is_bounded_without_changing_checkpoint():
    mw, req, _ = environment({"evidence_ids": [], "task_brief": {"task_id": "collect"}})
    for i in range(24):
        req.messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/workspace/evidence/d/E-1.json"},
                            "id": str(i),
                        }
                    ],
                ),
                ToolMessage(
                    name="read_file", tool_call_id=str(i), content="evidence " * 1000
                ),
            ]
        )
    req.state["messages"] = req.messages
    original = [m.model_dump() for m in req.messages]

    async def capture(request):
        assert sum(len(str(m.content)) for m in request.messages) < 30000
        assert len(request.messages) == len(req.messages)
        assert any("omitted" in str(m.content) for m in request.messages)

    await mw.awrap_model_call(req, capture)
    assert original == [m.model_dump() for m in req.messages]


@pytest.mark.asyncio
async def test_no_progress_removes_exploration_tools_before_next_model():
    mw, req, _ = environment(
        {
            "evidence_ids": [],
            "task_brief": {"task_id": "collect"},
            "no_progress_calls": 2,
        }
    )

    async def capture(request):
        assert request.tools == []
        assert "partial" in request.system_message.content

    await mw.awrap_model_call(req, capture)


@pytest.mark.asyncio
async def test_failed_query_does_not_stop_other_domains_and_counts_no_progress():
    from research_copilot.subagents.collection import (
        begin_source_call,
        finish_source_call,
        collection_stop_reason,
    )

    mw, req, state = environment(
        {"evidence_ids": [], "task_brief": {"task_id": "collect"}}
    )
    scope, _ = await mw.services.scope(None, "searcher")
    failed = {
        "name": "financekb_macro__get_macro_indicator",
        "args": {"q": "q"},
        "id": "failed",
    }
    await begin_source_call(mw.services, None, scope, failed)
    await finish_source_call(
        mw.services,
        None,
        scope,
        failed,
        ToolMessage(
            tool_call_id="failed",
            content=json.dumps(
                {"ok": False, "error": {"code": "timeout", "source": "financekb_macro"}}
            ),
        ),
    )
    material = state.role_data["scopes"]["d"]
    assert collection_stop_reason(material) is None
    assert material["no_progress_calls"] == 1
    assert material["source_calls"]["failed"]["new_evidence"] is False

    healthy = {"name": "financekb_news__search_news", "args": {"q": "q"}, "id": "ok"}
    await begin_source_call(mw.services, None, scope, healthy)
    await finish_source_call(
        mw.services,
        None,
        scope,
        healthy,
        ToolMessage(
            tool_call_id="ok",
            content=json.dumps({"ok": True, "new_evidence_ids": ["E-1"]}),
        ),
    )
    assert material["no_progress_calls"] == 0
    assert material["source_calls"]["ok"]["new_evidence"] is True


@pytest.mark.asyncio
async def test_token_headroom_triggers_wrap_up_before_call_limit():
    from research_copilot.runtime.budget import TokenBudget

    mw, req, state = environment(
        {"evidence_ids": [], "task_brief": {"task_id": "collect"}}
    )
    mw.budget = TokenBudget(
        limit=1000000,
        budget_id="b",
        stage_limits={"searcher": {"calls": 20, "tokens": 100000}},
        known_actual_tokens=60000,
        charged_tokens=60000,
        stages={
            "searcher": {
                "calls": 3,
                "known_actual_tokens": 60000,
                "unknown_reserved_tokens": 0,
            }
        },
    )
    state.role_data["model_audit"] = [
        {
            "dispatch_id": "d",
            "event": "started",
            "estimated_input_tokens": 25000,
            "max_output_tokens": 4000,
        }
    ]
    await mw.abefore_model(req.state, req.runtime)
    assert state.role_data["scopes"]["d"]["collection_stop_reason"] == "budget_wrap_up"
    assert mw.budget.snapshot()["known_actual_tokens"] == 60000


@pytest.mark.asyncio
async def test_wrap_up_still_allows_metric_registration_for_analysis_save():
    # 分析员收尾被预算收口时，save_analysis 之前必须还能登记指标回执，
    # 否则派发指定的 metric_id 永远判 Unknown metric receipt（live-catl 验收形态）。
    from types import SimpleNamespace

    from langchain_core.messages import ToolMessage

    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.runtime.control import RunControl
    from research_copilot.subagents.middleware import RoleBusinessMiddleware

    class Services:
        async def scope(self, context, role):
            return SimpleNamespace(
                dispatch_id="d", write_prefix="/workspace/tasks/d/"
            ), SimpleNamespace(role_data={"scopes": {"d": {}}})

        async def candidate(self, context, path, role):
            raise AssertionError("wrap-up must not expand beyond the allowed tools")

    middleware = RoleBusinessMiddleware("analysist", Services(), RunControl())
    middleware.budget = TokenBudget(
        limit=1000000,
        budget_id="b",
        stage_limits={"analysist": {"calls": 5, "tokens": 100000}},
        charged_tokens=0,
        stages={"analysist": {"calls": 4, "known_actual_tokens": 90000}},
    )

    async def _async_result(value):
        return value

    async def run(name):
        result = await middleware.awrap_tool_call(
            SimpleNamespace(
                runtime=SimpleNamespace(context=None),
                tool_call={"name": name, "args": {}, "id": name},
                state={"messages": []},
            ),
            handler=lambda request: _async_result(
                ToolMessage(
                    tool_call_id=request.tool_call["id"],
                    name=request.tool_call["name"],
                    content="{}",
                )
            ),
        )
        return result

    blocked = await run("execute")
    assert "budget_wrap_up" in blocked.content
    assert "budget_wrap_up" not in (await run("run_metric_specs")).content
    assert "budget_wrap_up" not in (await run("save_analysis")).content


def test_collection_model_turn_limit_scales_with_source_call_budget():
    # 来源调用次数约束采集广度；模型轮次还要覆盖读任务、检索、定位和收尾。
    from research_copilot.subagents.collection import collection_stop_reason

    scope = {"task_brief": {"max_source_calls": 5}, "collection_model_turns": 11}
    assert collection_stop_reason(scope) is None
    assert (
        collection_stop_reason({**scope, "collection_model_turns": 12})
        == "model_turn_limit"
    )


@pytest.mark.asyncio
async def test_wrap_up_compacts_history_when_full_request_cannot_fit_budget():
    # Tier A：完整历史预留超剩余，但最小收尾请求可预留 → 用压缩请求完成交付
    from research_copilot.runtime.budget import TokenBudget

    mw, req, state = environment(
        {
            "evidence_ids": ["E-1"],
            "task_brief": {"task_id": "collect"},
            "collection_stop_reason": "no_progress",
        }
    )
    req.messages = [
        AIMessage(content="证据内容" * 400, tool_calls=[]) for _ in range(10)
    ]
    req.state["messages"] = req.messages
    mw.budget = TokenBudget(
        limit=10**9,
        budget_id="b",
        stage_limits={"searcher": {"calls": 20, "tokens": 8000}},
        stages={
            "searcher": {
                "calls": 3,
                "known_actual_tokens": 0,
                "unknown_reserved_tokens": 0,
            }
        },
    )
    state.role_data["model_audit"] = [
        {
            "dispatch_id": "d",
            "event": "started",
            "estimated_input_tokens": 12000,
            "max_output_tokens": 4000,
        }
    ]

    async def capture(request):
        # 压缩后不应携带完整历史；模型按此请求预留可成功
        assert len(request.messages) <= 2
        assert "SearchResult" in request.messages[-1].content

    await mw.awrap_model_call(req, capture)


@pytest.mark.asyncio
async def test_budget_deadlock_delivers_partial_without_model_call():
    # Tier B：连最小收尾请求都无法预留 → 直接合成 partial 结果，不再调用模型
    from research_copilot.runtime.budget import TokenBudget

    mw, req, state = environment(
        {
            "evidence_ids": ["E-1", "E-2"],
            "task_brief": {"task_id": "collect", "required_fields": ["营收"]},
        }
    )
    mw.budget = TokenBudget(
        limit=10**9,
        budget_id="b",
        stage_limits={"searcher": {"calls": 20, "tokens": 1000}},
        stages={
            "searcher": {
                "calls": 3,
                "known_actual_tokens": 900,
                "unknown_reserved_tokens": 0,
            }
        },
    )
    state.role_data["model_audit"] = [
        {
            "dispatch_id": "d",
            "event": "started",
            "estimated_input_tokens": 800,
            "max_output_tokens": 4000,
        }
    ]
    outcome = await mw.abefore_model(req.state, req.runtime)
    assert outcome["jump_to"] == "end"
    result = outcome["structured_response"]
    assert result.status == "partial"
    assert result.evidence_ids == ["E-1", "E-2"]
    assert any("营收" in gap.description for gap in result.gaps)
    assert mw.budget.calls == 0  # 未发生任何模型预留
    assert mw.services.mapped  # 结果经过契约校验


@pytest.mark.asyncio
async def test_truncated_search_submission_gets_one_wrapup_then_keeps_sources():
    mw, req, state = environment(
        {
            "evidence_ids": ["E-1"],
            "task_brief": {"task_id": "collect", "required_fields": ["revenue"]},
        }
    )
    req.messages = [
        AIMessage(
            content="现在提交事实清单。",
            invalid_tool_calls=[
                {
                    "name": "SearchResult",
                    "args": '{"facts":[',
                    "id": "cut",
                    "error": None,
                }
            ],
            response_metadata={"finish_reason": "length"},
        )
    ]
    req.state["messages"] = req.messages
    retry = await mw.aafter_model(req.state, req.runtime)
    assert retry and retry["jump_to"] == "model"
    req.state["retry_truncated_search"] = retry["retry_truncated_search"]
    await mw.abefore_model(req.state, req.runtime)

    async def capture(request):
        assert request.tools == []
        assert not any(getattr(m, "invalid_tool_calls", []) for m in request.messages)

    await mw.awrap_model_call(req, capture)
    final = await mw.aafter_model(req.state, req.runtime)
    assert final["jump_to"] == "end"
    result = final["structured_response"]
    assert json.loads(final["messages"][-1].content) == result.model_dump(mode="json")
    assert result.status == "partial" and result.evidence_ids == ["E-1"]
    assert "revenue" in result.gaps[0].description


@pytest.mark.asyncio
async def test_wrapup_injects_verbatim_excerpts_from_late_original_source():
    # A large original fetched on the last exploration turn is index-only during
    # exploration, but wrap-up has no tools left; excerpts must carry the figures.
    from research_copilot.subagents.services import digest

    binding = {
        "evidence_ids": ["snippet", "annual"],
        "task_brief": {
            "task_id": "collect",
            "required_fields": ["revenue_2024"],
            "topic_terms": ["营业收入", "经营活动现金流量净额"],
        },
        "collection_stop_reason": "budget_wrap_up",
    }
    mw, req, state = environment(binding)
    evidence = state.role_data["evidence"] = {}
    docs = [
        ("snippet", "search_snippet", "2024年年度报告摘要", "营业收入 999"),
        (
            "annual",
            "original_text",
            "宁德时代2024年年度报告",
            "背景说明" * 5000 + "营业收入 123456 经营活动现金流量净额 7890",
        ),
    ]
    for ident, kind, title, line in docs:
        raw = {"title": title, "body": line}
        evidence[ident] = {"kind": kind, "raw": raw, "content_hash": digest(raw)}

    async def capture(request):
        assert request.tools == []
        prompt = request.system_message.content
        assert "营业收入 123456 经营活动现金流量净额 7890" in prompt
        assert "search_snippet 仅用于定位" in prompt

    await mw.awrap_model_call(req, capture)


@pytest.mark.asyncio
async def test_correction_wrapup_injects_original_quarter_figures_before_early_snippets():
    from research_copilot.subagents.source_context import source_context
    from research_copilot.subagents.services import digest

    brief = {
        "task_id": "collect",
        "required_fields": ["revenue_2025q1", "cost_2025q2"],
        "topic_terms": ["营业收入", "营业成本"],
    }
    docs = [
        ("snippet", "search_snippet", "2025年度报告", "年度收入 888"),
        ("prior", "original_text", "2024年第一季度报告", "营业收入 777 营业成本 666"),
        ("half", "original_text", "2025年半年度报告", "营业收入 23456 营业成本 7890"),
        (
            "first",
            "original_text",
            "2025年第一季度报告",
            "营业收入 12345 营业成本 6789",
        ),
        ("private", "original_text", "2025年第一季度报告", "营业收入 private-secret"),
    ]
    binding = {
        "evidence_ids": [d[0] for d in docs[:-1]],
        "task_brief": brief,
        "correction_of": "original",
        "collection_stop_reason": "budget_wrap_up",
    }
    mw, req, state = environment(binding)
    evidence = state.role_data["evidence"] = {}
    for ident, kind, title, line in docs:
        raw = {"title": title, "body": "背景" * 5500 + line}
        evidence[ident] = {"kind": kind, "raw": raw, "content_hash": digest(raw)}

    async def capture(request):
        assert request.tools == []
        prompt = request.system_message.content
        assert (
            "营业收入 12345 营业成本 6789" in prompt
            and "营业收入 23456 营业成本 7890" in prompt
        )
        assert "private-secret" not in prompt

    await mw.awrap_model_call(req, capture)
    rows = source_context(state, binding)
    assert {r["evidence_id"] for r in rows[:2]} == {"first", "half"}
    for row in rows[:2]:
        raw = evidence[row["evidence_id"]]["raw"]
        assert row["content_hash"] == digest(raw)
        span = next(s for s in row["excerpts"] if "营业收入" in s["text"])
        assert span["pointer"] == "/body"
        assert raw["body"][span["start"] : span["end"]] == span["text"]
    assert len(json.dumps(rows, ensure_ascii=False).encode()) <= 8192


@pytest.mark.asyncio
async def test_wrapup_validation_failure_gets_one_bounded_repair_turn():
    from research_copilot.subagents.middleware import REPAIR_MARKER

    mw, req, state = environment(
        {
            "evidence_ids": [],
            "task_brief": {"task_id": "collect", "max_source_calls": 8},
            "collection_stop_reason": "model_turn_limit",
            "collection_model_turns": 10,
            "collection_wrap_up_turns": 2,
        }
    )
    req.state["messages"] = [
        ToolMessage(tool_call_id="c", content=REPAIR_MARKER + "quote must be unique")
    ]
    assert await mw.abefore_model(req.state, req.runtime) == {"role_repair_turns": 1}
    assert state.role_data["scopes"]["d"]["collection_repair_turns"] == 1

    req.state["messages"] = []
    blocked = await mw.abefore_model(req.state, req.runtime)
    assert blocked["jump_to"] == "end"
    assert blocked["structured_response"].status == "partial"


@pytest.mark.asyncio
async def test_execute_budget_exceeded_returns_structured_error(tmp_path):
    # WLY-20261107 验收缺陷：execute 的 BudgetExceeded 曾砸穿子图留下悬挂工具调用，
    # 模型看不到被拒原因；必须转为结构化工具错误回执。
    from research_copilot.runtime.budget import BudgetExceeded

    state = SimpleNamespace(
        role_data={"scopes": {"d": {"task_brief": {"calculation_mode": "custom"}}}}
    )
    script = tmp_path / "tasks" / "d" / "code" / "chart_core.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('chart')\n", encoding="utf-8")

    class Services:
        def report_root(self, context):
            return tmp_path

        async def scope(self, context, role):
            return SimpleNamespace(
                dispatch_id="d", write_prefix="/workspace/tasks/d/"
            ), state

    async def handler(request):
        raise BudgetExceeded(
            "Additional successful execution needs a frozen task purpose"
        )

    middleware = RoleBusinessMiddleware("analysist", Services(), RunControl())
    request = SimpleNamespace(
        tool_call={
            "id": "exec-1",
            "name": "execute",
            "args": {"command": "python /workspace/tasks/d/code/chart_core.py"},
        },
        runtime=SimpleNamespace(context=None),
        state={"messages": []},
    )
    response = await middleware.awrap_tool_call(request, handler)
    assert isinstance(response, ToolMessage)
    payload = json.loads(response.content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "execution_blocked"
    assert "frozen task purpose" in payload["error"]["message"]
    assert payload["error"]["retryable"] is False
    assert payload["error"]["container_started"] is False


@pytest.mark.asyncio
async def test_execute_cancellation_still_propagates(tmp_path):
    # 取消与运行控制必须继续向上传播，不得被 execute 的错误包装吞掉。
    import asyncio

    state = SimpleNamespace(
        role_data={"scopes": {"d": {"task_brief": {"calculation_mode": "custom"}}}}
    )
    script = tmp_path / "tasks" / "d" / "code" / "chart_core.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('chart')\n", encoding="utf-8")

    class Services:
        def report_root(self, context):
            return tmp_path

        async def scope(self, context, role):
            return SimpleNamespace(
                dispatch_id="d", write_prefix="/workspace/tasks/d/"
            ), state

    async def handler(request):
        raise asyncio.CancelledError()

    middleware = RoleBusinessMiddleware("analysist", Services(), RunControl())
    request = SimpleNamespace(
        tool_call={
            "id": "exec-2",
            "name": "execute",
            "args": {"command": "python /workspace/tasks/d/code/chart_core.py"},
        },
        runtime=SimpleNamespace(context=None),
        state={"messages": []},
    )
    with pytest.raises(asyncio.CancelledError):
        await middleware.awrap_tool_call(request, handler)


@pytest.mark.asyncio
async def test_truncated_analysis_retry_strips_invalid_tool_calls():
    # WLY 第二轮验收缺陷：截断轮残留 invalid_tool_calls 时，langchain 下一轮会把
    # 它们补丁成孤儿 ToolMessage，DeepSeek 以 400（tool 消息失去配对）拒绝整个会话。
    from langchain_core.messages import AIMessage

    middleware = RoleBusinessMiddleware("analysist", SimpleNamespace(), RunControl())
    truncated = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "write_file",
                "args": '{"path": "/work',
                "id": "call-1",
                "error": "Malformed args",
            }
        ],
        response_metadata={"finish_reason": "length"},
    )
    state = {"messages": [truncated]}
    update = await middleware.aafter_model(state, SimpleNamespace())
    assert update["retry_truncated_analysis"] is True
    assert update["jump_to"] == "model"
    cleaned = update["messages"][0]
    assert isinstance(cleaned, AIMessage)
    assert cleaned.invalid_tool_calls == []
    assert cleaned.id == truncated.id


@pytest.mark.asyncio
async def test_truncated_search_retry_strips_invalid_tool_calls():
    # 同一断裂机制在 searcher 收尾截断重试路径的加固。
    from langchain_core.messages import AIMessage

    middleware = RoleBusinessMiddleware("searcher", SimpleNamespace(), RunControl())
    truncated = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "web_search",
                "args": '{"query"',
                "id": "call-2",
                "error": "Malformed args",
            }
        ],
        response_metadata={"finish_reason": "length"},
    )
    state = {"messages": [truncated]}
    update = await middleware.aafter_model(state, SimpleNamespace())
    assert update["retry_truncated_search"] is True
    assert update["jump_to"] == "model"
    cleaned = update["messages"][0]
    assert isinstance(cleaned, AIMessage)
    assert cleaned.invalid_tool_calls == []
    assert cleaned.id == truncated.id
