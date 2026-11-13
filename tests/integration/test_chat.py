"""Conversation and transport acceptance with real graph/SQLite, no paid models."""

import hashlib

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import RunContext
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord, SessionService
from research_copilot.runtime.storage import SqlSessionStore


@pytest.fixture
async def chat_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord(
            "chat-1", "local", "v2", "policy-1", "chat-1", user_request="分析毛利率"
        ),
        TokenBudget(limit=100000, budget_id="chat-1").snapshot(),
    )

    async def ask(state):
        if state["messages"][-1].content == "完成":
            return {"messages": [AIMessage(content="本轮已完成。")]}
        return {
            "messages": [
                AIMessage(
                    content="请补充要比较的季度。",
                    additional_kwargs={"awaiting_user_input": True},
                )
            ]
        }

    graph = StateGraph(MessagesState)
    graph.add_node("reply", ask)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    agent = graph.compile(checkpointer=InMemorySaver())
    session = SessionService(
        store=store,
        agent_factory=lambda record, control: agent,
        policy_version="policy-1",
    )
    yield store, session, tmp_path
    await engine.dispose()


@pytest.mark.asyncio
async def test_clarification_and_followup_survive_reload_without_approval(chat_env):
    store, session, _ = chat_env
    with pytest.raises(ValueError, match="尚未启动"):
        await session.message("chat-1", "local", "补充", "before-start")
    result = await session.start("chat-1", "local", "分析毛利率")
    assert result.status == "failed"
    await session.message("chat-1", "local", "比较2025Q1和2025Q2", "user-followup")
    record = await store.load("chat-1", "local")
    context = RunContext("local", "chat-1", record.run_id, "chat-1", "policy-1")
    state = await store.read_control(context)
    messages = state.role_data["conversation"]
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "分析毛利率",
        "比较2025Q1和2025Q2",
    ]
    assert messages[-1]["content"] == "请补充要比较的季度。"
    assert not state.approved
    before = len(messages)
    await session.message("chat-1", "local", "比较2025Q1和2025Q2", "user-followup")
    assert len((await store.read_control(context)).role_data["conversation"]) == before
    with pytest.raises(ValueError, match="identity"):
        await session.message("chat-1", "local", "changed", "user-followup")


@pytest.mark.asyncio
async def test_frozen_scope_accepts_followup_but_never_changes(chat_env):
    from research_copilot.runtime.autonomy import freeze_scope

    store, session, _ = chat_env
    await session.start("chat-1", "local", "分析毛利率")
    record = await store.load("chat-1", "local")
    context = RunContext("local", "chat-1", record.run_id, "chat-1", "policy-1")
    scope = {"report_type": "company_report", "objective": "分析毛利率", "as_of": "2026-11-12"}

    def activate(state):
        state.role_data["workflow_version"] = "products-v2"
        return freeze_scope(state, "chat-1", scope)

    await store.mutate_control(context, activate)
    await session.message("chat-1", "local", "继续", "user-after-freeze")
    state = await store.read_control(context)
    assert [m["content"] for m in state.role_data["conversation"] if m["role"] == "user"] == [
        "分析毛利率",
        "继续",
    ]
    with pytest.raises(ValueError, match="frozen"):
        await store.mutate_control(
            context, lambda s: freeze_scope(s, "chat-1", dict(scope, objective="改写范围"))
        )


@pytest.mark.asyncio
async def test_old_question_does_not_keep_finished_turn_waiting(chat_env):
    _, session, _ = chat_env
    assert (await session.start("chat-1", "local", "分析毛利率")).status == "failed"
    assert (
        await session.message("chat-1", "local", "完成", "user-done")
    ).status == "failed"


@pytest.mark.asyncio
async def test_native_clarification_uses_explicit_interrupt(chat_env):
    from tests.unit.test_orchestration import ScriptModel, call, assemble
    from research_copilot.runtime.business import BusinessCoordinator

    store, _, root = chat_env
    model = ScriptModel(
        script=[
            call("ask_user", {"question": "请补充比较的季度。"}),
            AIMessage(content="已收到"),
        ]
    )
    graph = assemble(
        root,
        model,
        autonomous_workflow=True,
        report_id="chat-1",
        budget=TokenBudget(limit=100000, budget_id="chat-1"),
    )
    store.resume_validator = BusinessCoordinator(store).validate_resume
    session = SessionService(
        store=store,
        agent_factory=lambda record, control: graph,
        policy_version="policy-1",
    )
    first = await session.start("chat-1", "local", "分析毛利率")
    assert first.status == "paused"
    await session.resume(
        "chat-1", "local", {first.interrupts[0]["id"]: {"answer": "2025Q1与Q2"}}
    )
    assert model.position == 2


@pytest.mark.asyncio
async def test_sse_transmits_committed_report_and_resumes_by_offset(chat_env):
    from research_copilot.contracts import ArtifactRef
    from research_copilot.web.app import WorkbenchBackend
    from research_copilot.web.stream import workspace_events

    store, session, root = chat_env
    await session.start("chat-1", "local", "分析毛利率")
    record = await store.load("chat-1", "local")
    context = RunContext("local", "chat-1", record.run_id, "chat-1", "policy-1")
    text = "# 分析\n\n" + ("中文结论与来源。" * 500)
    ref = ArtifactRef(artifact_id="report-1", version="1", content_hash="a" * 64)

    def seed(state):
        state.artifacts["report"] = ref
        state.role_data["objects"] = {
            "report-1": {
                "kind": "report",
                "ref": ref.model_dump(),
                "payload": {"document": text},
            }
        }

    await store.mutate_control(context, seed)
    backend = WorkbenchBackend(
        session=session, store=store, actor_id="local", workspace_root=root
    )

    async def connected():
        return False

    stream = workspace_events(backend, "chat-1", connected, poll_interval=0.01)
    received = []
    try:
        while True:
            frame = await anext(stream)
            received.append(frame)
            if "event: report.complete" in frame:
                break
    finally:
        await stream.aclose()
    import json

    chunks = [
        json.loads(frame.split("data: ", 1)[1])
        for frame in received
        if "event: report.delta" in frame
    ]
    assert "".join(c["text"] for c in chunks) == text
    assert len(chunks) > 1 and chunks[0]["offset"] == 0
    digest = hashlib.sha256(text.encode()).hexdigest()
    resumed = workspace_events(
        backend,
        "chat-1",
        connected,
        last_event_id=f"report:{digest}:1024",
        poll_interval=0.01,
    )
    try:
        frames = []
        while True:
            frame = await anext(resumed)
            frames.append(frame)
            if "event: report.complete" in frame:
                break
        deltas = [
            json.loads(f.split("data: ", 1)[1])
            for f in frames
            if "event: report.delta" in f
        ]
        assert "".join(d["text"] for d in deltas) == text[1024:]
    finally:
        await resumed.aclose()


@pytest.mark.asyncio
async def test_chat_api_deduplicates_messages_and_enforces_actor_scope_and_plan_gate(
    chat_env,
):
    import httpx

    from research_copilot.contracts import ArtifactRef
    from research_copilot.web.app import WorkbenchBackend, create_app

    store, session, root = chat_env
    await session.start("chat-1", "local", "分析毛利率")
    backend = WorkbenchBackend(
        session=session, store=store, actor_id="local", workspace_root=root
    )
    app = create_app(backend=backend)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (
            await client.post(
                "/api/reports/chat-1/messages", json={"content": "", "id": "m-1"}
            )
        ).status_code == 400
        assert (await client.get("/api/reports/missing/events")).status_code == 404
        backend.actor_id = "another-actor"
        assert (await client.get("/api/reports/chat-1/events")).status_code == 404
        assert (
            await client.post(
                "/api/reports/chat-1/messages", json={"content": "补充", "id": "m-1"}
            )
        ).status_code == 404
        backend.actor_id = "local"
        payload = {"content": "比较2025Q1和2025Q2", "id": "m-1"}
        assert (
            await client.post("/api/reports/chat-1/messages", json=payload)
        ).status_code == 200
        await backend.runs.drain()
        before = (await client.get("/api/reports/chat-1")).json()
        assert (
            await client.post("/api/reports/chat-1/messages", json=payload)
        ).status_code == 200
        assert (await client.get("/api/reports/chat-1")).json()["messages"] == before[
            "messages"
        ]
        assert (
            await client.post(
                "/api/reports/chat-1/messages", json={**payload, "content": "different"}
            )
        ).status_code == 409
        record = await store.load("chat-1", "local")
        context = RunContext("local", "chat-1", record.run_id, "chat-1", "policy-1")
        await store.mutate_control(
            context,
            lambda state: state.artifacts.update(
                plan=ArtifactRef(artifact_id="p-1", version="1", content_hash="b" * 64)
            ),
        )
        assert (
            await client.post(
                "/api/reports/chat-1/messages", json={"content": "批准", "id": "m-2"}
            )
        ).status_code == 409
        assert not (await store.read_control(context)).approved
    await backend.runs.aclose()


@pytest.mark.asyncio
async def test_busy_message_is_rejected_before_inspecting_running_agent(chat_env):

    import httpx

    from research_copilot.web.app import WorkbenchBackend, create_app

    store, session, root = chat_env
    await session.start("chat-1", "local", "分析毛利率")

    async def forbidden(*args):
        raise AssertionError("A busy message must not reconstruct an active agent")

    session.get_status = forbidden
    backend = WorkbenchBackend(
        session=session, store=store, actor_id="local", workspace_root=root
    )
    await backend.runs.queue.enqueue(
        report_id="chat-1",
        actor_id="local",
        operation_id="busy-command",
        kind="resume",
        payload={"decision": None},
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(backend=backend)),
            base_url="http://localhost",
        ) as client:
            assert (
                await client.post(
                    "/api/reports/chat-1/messages",
                    json={"content": "补充", "id": "busy-input"},
                )
            ).status_code == 409
    finally:
        await backend.runs.cancel_waiting("chat-1")
        await backend.runs.aclose()


@pytest.mark.asyncio
async def test_pending_interrupt_cannot_be_approved_by_chat_message(chat_env):
    from langgraph.types import interrupt

    store, _, _ = chat_env

    def gated(state):
        interrupt({"action_requests": [{"name": "confirm_plan", "args": {}}]})

    graph = StateGraph(MessagesState)
    graph.add_node("gate", gated)
    graph.add_edge(START, "gate")
    graph.add_edge("gate", END)
    agent = graph.compile(checkpointer=InMemorySaver())
    session = SessionService(
        store=store,
        agent_factory=lambda record, control: agent,
        policy_version="policy-1",
    )
    assert (await session.start("chat-1", "local", "分析毛利率")).status == "paused"
    with pytest.raises(ValueError, match="待确认"):
        await session.message("chat-1", "local", "批准", "m-approve")


@pytest.mark.asyncio
async def test_stream_resets_when_current_report_is_invalidated(chat_env):
    import asyncio

    from research_copilot.contracts import ArtifactRef
    from research_copilot.web.app import WorkbenchBackend
    from research_copilot.web.stream import workspace_events

    store, session, root = chat_env
    await session.start("chat-1", "local", "分析毛利率")
    record = await store.load("chat-1", "local")
    context = RunContext("local", "chat-1", record.run_id, "chat-1", "policy-1")
    ref = ArtifactRef(
        artifact_id="committed-report", version="1", content_hash="b" * 64
    )

    def commit(state):
        state.artifacts["report"] = ref
        state.role_data["objects"] = {
            ref.artifact_id: {
                "kind": "report",
                "ref": ref.model_dump(),
                "payload": {"document": "已提交正文"},
            }
        }

    await store.mutate_control(context, commit)
    backend = WorkbenchBackend(
        session=session, store=store, actor_id="local", workspace_root=root
    )

    async def connected():
        return False

    stream = workspace_events(backend, "chat-1", connected, poll_interval=0.01)
    try:
        while "event: report.complete" not in await asyncio.wait_for(anext(stream), 2):
            pass
        updated = ArtifactRef(
            artifact_id="report-revision-2", version="2", content_hash="c" * 64
        )

        def revise(state):
            state.artifacts["report"] = updated
            state.role_data["objects"][updated.artifact_id] = {
                "kind": "report",
                "ref": updated.model_dump(),
                "payload": {"document": "已提交正文"},
            }

        await store.mutate_control(context, revise)
        frames = [
            await asyncio.wait_for(anext(stream), 2),
            await asyncio.wait_for(anext(stream), 2),
        ]
        assert any(
            "event: report.start" in event and '"version": "2"' in event
            for event in frames
        )
        while "event: report.complete" not in await asyncio.wait_for(anext(stream), 2):
            pass
        await store.mutate_control(context, lambda state: state.artifacts.pop("report"))
        frames = [
            await asyncio.wait_for(anext(stream), 2),
            await asyncio.wait_for(anext(stream), 2),
        ]
        assert any("event: report.reset" in event for event in frames)
        assert any(event.startswith("id: \nevent: report.reset") for event in frames)
        from research_copilot.web.workspace import select_report

        assert (
            select_report(await store.read_control(context), "1")["document"]
            == "已提交正文"
        )
    finally:
        await stream.aclose()
