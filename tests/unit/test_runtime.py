import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langgraph.graph import START, END, MessagesState, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt


@pytest.mark.asyncio
async def test_budget_reservation_prevents_overspend_and_unknown_usage_is_not_free():
    from research_copilot.runtime.budget import BudgetExceeded, TokenBudget

    budget = TokenBudget(limit=100, budget_id="b")
    first = await budget.reserve(60)
    with pytest.raises(BudgetExceeded):
        await budget.reserve(41)
    await budget.settle(first, usage=None)
    assert budget.snapshot()["unknown_requests"] == 1
    assert budget.snapshot()["charged_tokens"] == 60
    with pytest.raises(BudgetExceeded):
        await budget.reserve(41)


@pytest.mark.asyncio
async def test_cancellation_drains_active_dispatch_and_never_starts_queue():
    from research_copilot.contracts import Dispatch
    from research_copilot.runtime.control import RunCancelled, RunControl

    control = RunControl(max_parallel_tasks=1)
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def work(ident):
        async with control.dispatch(
            Dispatch(ident, "searcher", f"/workspace/tasks/{ident}/")
        ):
            entered.set()
            await release.wait()
            completed.append(ident)

    first = asyncio.create_task(work("one"))
    await entered.wait()
    queued = asyncio.create_task(work("two"))
    await asyncio.sleep(0)
    control.cancel()
    release.set()
    await first
    with pytest.raises(RunCancelled):
        await queued
    assert completed == ["one"]


@pytest.mark.asyncio
async def test_persistent_cancellation_is_checked_before_dispatch():
    from research_copilot.contracts import Dispatch
    from research_copilot.runtime.control import RunCancelled, RunControl

    async def cancelled():
        return True

    control = RunControl(cancellation_probe=cancelled)
    with pytest.raises(RunCancelled):
        async with control.dispatch(Dispatch("d", "writer", "/workspace/tasks/d/")):
            pytest.fail("Cancelled task started")


@pytest.mark.asyncio
async def test_cancelled_queue_does_not_abort_inflight_result_collection():
    from research_copilot.orchestration.middleware import DispatchMiddleware
    from research_copilot.runtime.control import RunControl
    from langchain_core.messages import ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from tests.unit.test_orchestration import Business, context

    business, control = Business(), RunControl()
    middleware = DispatchMiddleware(business, control, "report-1")
    entered, release = asyncio.Event(), asyncio.Event()
    active = []

    async def handler(request):
        active.append(request.tool_call["id"])
        if len(active) == 3:
            entered.set()
        await release.wait()
        return ToolMessage(
            content='{"role":"searcher","status":"unavailable","summary":"offline","references":[],"gaps":["unavailable"]}',
            tool_call_id=request.tool_call["id"],
        )

    requests = [
        ToolCallRequest(
            tool=None,
            state={},
            runtime=SimpleNamespace(context=context()),
            tool_call={
                "name": "task",
                "id": str(i),
                "args": {"subagent_type": "searcher", "description": "collect"},
            },
        )
        for i in range(4)
    ]
    tasks = [
        asyncio.create_task(middleware.awrap_tool_call(request, handler))
        for request in requests
    ]
    await entered.wait()
    control.cancel()
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(active) == 3
    assert sum(result.status == "error" for result in results) == 1


class Sessions:
    def __init__(self):
        from research_copilot.runtime.business import ControlState

        self.control = ControlState(role_data={"workflow_version": "products-v2"})
        self.record = None
        self.locked = False

    async def read_control(self, context):
        return self.control

    async def mutate_control(self, context, operation):
        return operation(self.control)

    async def load(self, report_id, actor_id):
        if actor_id != "actor-1":
            raise PermissionError("wrong actor")
        if self.record is None:
            raise LookupError(report_id)
        return self.record

    @asynccontextmanager
    async def lock(self, report_id):
        if self.locked:
            raise RuntimeError("busy")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    async def save(self, record):
        self.record = record

    async def validate_resume(self, record, actor_id, pending, decisions):
        if any(value.get("stale") for value in decisions.values()):
            raise ValueError("stale artifact")

    async def request_cancel(self, report_id, actor_id):
        self.record = replace(self.record, cancel_requested=True)


def setup_service():
    from research_copilot.runtime.session import SessionRecord, SessionService

    store = Sessions()
    store.record = SessionRecord(
        report_id="report-1",
        actor_id="actor-1",
        runtime_version="v2",
        policy_version="policy-1",
        budget_id="budget-1",
    )
    saver = InMemorySaver()
    calls = []

    def build(record, control):
        async def node(state):
            calls.append(state["messages"][-1].content)
            interrupt(
                {
                    "kind": "reviewer_deep_review",
                    "snapshot": {"version": "v1"},
                    "allowed_decisions": ["approve", "request_changes", "reject"],
                }
            )
            return {}

        graph = StateGraph(MessagesState)
        graph.add_node("review", node)
        graph.add_edge(START, "review")
        graph.add_edge("review", END)
        return graph.compile(checkpointer=saver)

    return (
        SessionService(store=store, agent_factory=build, policy_version="policy-1"),
        store,
        calls,
    )


@pytest.mark.asyncio
async def test_session_resumes_by_interrupt_id_and_rejects_stale_unknown_or_duplicate_start():
    service, store, calls = setup_service()
    first = await service.start("report-1", "actor-1", "research")
    assert first.status == "paused"
    pending = first.interrupts[0]
    with pytest.raises(ValueError, match="already"):
        await service.start("report-1", "actor-1", "duplicate")
    with pytest.raises(ValueError, match="interrupt"):
        await service.resume("report-1", "actor-1", {"wrong": {"decision": "approve"}})
    with pytest.raises(ValueError, match="stale"):
        await service.resume(
            "report-1",
            "actor-1",
            {pending["id"]: {"decision": "approve", "stale": True}},
        )
    assert (await service.get_status("report-1", "actor-1")).status == "paused"
    result = await service.resume(
        "report-1", "actor-1", {pending["id"]: {"decision": "approve"}}
    )
    assert result.status == "failed"
    assert calls == [
        "research",
        "research",
    ]  # interrupt node resumes, no new input appended


@pytest.mark.asyncio
async def test_session_rejects_v1_wrong_actor_and_policy_change_before_graph_execution():
    service, store, calls = setup_service()
    with pytest.raises(PermissionError):
        await service.start("report-1", "other", "research")
    store.record = replace(store.record, runtime_version="v1")
    with pytest.raises(ValueError, match="V1"):
        await service.start("report-1", "actor-1", "research")
    store.record = replace(store.record, runtime_version="v2", policy_version="old")
    with pytest.raises(ValueError, match="policy"):
        await service.start("report-1", "actor-1", "research")
    assert calls == []


@pytest.mark.asyncio
async def test_resume_rebuilds_from_checkpoint_after_service_restart():
    from research_copilot.runtime.session import SessionService

    service, store, _ = setup_service()
    first = await service.start("report-1", "actor-1", "research")
    restarted = SessionService(
        store=store, agent_factory=service.agent_factory, policy_version="policy-1"
    )
    result = await restarted.resume(
        "report-1",
        "actor-1",
        {
            first.interrupts[0]["id"]: {
                "decision": "request_changes",
                "comment": "fix numbers",
            }
        },
    )
    assert result.status == "failed"
    assert store.record.budget_id == "budget-1"


@pytest.mark.asyncio
async def test_explicit_resume_retries_failed_checkpoint_without_restarting_the_request():
    from research_copilot.runtime.session import SessionRecord, SessionService

    store = Sessions()
    store.record = SessionRecord("report-1", "actor-1", "v2", "policy-1", "budget-1")
    saver = InMemorySaver()
    calls = []

    def factory(record, control):
        async def node(state):
            calls.append(state["messages"][-1].content)
            if len(calls) == 1:
                raise RuntimeError("transient transport failure")
            return {}

        graph = StateGraph(MessagesState)
        graph.add_node("work", node)
        graph.add_edge(START, "work")
        graph.add_edge("work", END)
        return graph.compile(checkpointer=saver)

    service = SessionService(
        store=store, agent_factory=factory, policy_version="policy-1"
    )
    with pytest.raises(RuntimeError, match="transient"):
        await service.start("report-1", "actor-1", "research")
    assert store.record.status == "failed"
    result = await service.resume("report-1", "actor-1", None)
    assert result.status == "failed"
    assert calls == ["research", "research"]
