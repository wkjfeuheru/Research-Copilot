import pytest
from research_copilot.runtime.session import SessionService, SessionRecord
from tests.unit.test_business import ControlRepository


class Store(ControlRepository):
    async def load(self, report_id, actor_id):
        return SessionRecord(
            report_id, actor_id, "v2", "policy", "budget", run_id="run"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "products-v1", "supervisor-v1"])
async def test_history_status_does_not_construct_graph_and_mutations_rejected(version):
    store = Store()
    if version:
        store.state.role_data["workflow_version"] = version

    def forbidden(*args):
        raise AssertionError("Historical reads must not construct a graph")

    session = SessionService(
        store=store, agent_factory=forbidden, policy_version="policy"
    )
    status = await session.get_status("report", "actor")
    assert status.status == "read_only"
    record = await store.load("report", "actor")
    with pytest.raises(PermissionError, match="read.only"):
        await session._start(record, "research")
    with pytest.raises(PermissionError, match="read.only"):
        await session._resume(record, None)


def test_clarification_projection_is_not_an_approval():
    from research_copilot.web.workspace import normalize_interrupts

    item = normalize_interrupts(
        [{"id": "q", "value": {"kind": "clarification", "question": "Which company?"}}]
    )[0]
    assert item["kind"] == "clarification"
    assert item["question"] == "Which company?"
    assert not item["actions"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,continues",
    [
        # 一次失败续跑会把 last_run_error 覆盖为异常类型，仍须可恢复
        ("ValueError", True),
        ("RuntimeError", True),
        ("IncompleteRun", True),
        # 主动阻断类失败必须人工处置，不能被续跑绕过
        ("WorkflowBlocked", False),
        ("ExecutionUnknown", False),
    ],
)
async def test_resume_gate_follows_failure_code(code, continues):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from research_copilot.runtime.budget import TokenBudget

    store = Store()
    store.state.role_data.update(
        workflow_version="products-v2", last_run_error={"code": code}
    )
    record = replace(await store.load("report", "actor"), status="failed")
    snapshot = SimpleNamespace(
        interrupts=(), next=(), values={}, created_at="persisted", tasks=()
    )
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot))
    session = SessionService(
        store=store, agent_factory=lambda *_: graph, policy_version="policy"
    )
    session._invoke = AsyncMock()
    store.load_budget = AsyncMock(
        return_value=TokenBudget(limit=1000, budget_id="budget")
    )
    if continues:
        await session._resume(record, None)
        assert session._invoke.call_args.args[1]["messages"][0]["id"] == "resume:report"
    else:
        with pytest.raises(ValueError, match="interrupt"):
            await session._resume(record, None)
        session._invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_skips_answered_interrupt_metadata(tmp_path):
    """已答 clarification 残留在 checkpoint 时，resume 应排除而不是要求重复回答。"""
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from research_copilot.runtime.budget import TokenBudget

    store = Store()
    store.state.role_data.update(
        workflow_version="products-v2",
        last_run_error={"code": "IncompleteRun"},
        conversation=[
            {
                "id": "answer:resolved-interrupt",
                "role": "user",
                "content": "需要配图",
                "kind": "text",
                "created_at": "2026-11-08T00:00:00+00:00",
            }
        ],
    )
    record = replace(await store.load("report", "actor"), status="failed")
    snapshot = SimpleNamespace(
        interrupts=[
            SimpleNamespace(
                id="resolved-interrupt",
                value={"kind": "clarification", "question": "Which company?"},
            )
        ],
        next=(),
        values={"messages": ["premature answer"]},
        created_at="persisted",
        tasks=(),
    )
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot))
    session = SessionService(
        store=store, agent_factory=lambda *_: graph, policy_version="policy"
    )
    session._invoke = AsyncMock()
    store.load_budget = AsyncMock(
        return_value=TokenBudget(limit=1000, budget_id="budget")
    )
    await session._resume(record, None)
    value = session._invoke.call_args.args[1]
    assert value["messages"][0]["id"] == "resume:report"


@pytest.mark.asyncio
async def test_explicit_resume_of_premature_stop_retains_checkpoint_and_identity():
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from research_copilot.runtime.budget import TokenBudget

    store = Store()
    store.state.role_data.update(
        workflow_version="products-v2", last_run_error={"code": "IncompleteRun"}
    )
    record = replace(await store.load("report", "actor"), status="failed")
    snapshot = SimpleNamespace(
        interrupts=(),
        next=(),
        values={"messages": ["premature answer"]},
        created_at="persisted",
        tasks=(),
    )
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot))
    session = SessionService(
        store=store, agent_factory=lambda *_: graph, policy_version="policy"
    )
    session._invoke = AsyncMock()
    store.load_budget = AsyncMock(
        return_value=TokenBudget(limit=1000, budget_id="budget")
    )
    await session._resume(record, None)
    actual, value = session._invoke.call_args.args
    assert actual.run_id == record.run_id and actual.budget_id == record.budget_id
    assert value["messages"][0]["role"] == "user"
    # resume 指令必须带专属稳定 id，避免与首轮 user_request 撞 ID
    assert value["messages"][0]["id"] == "resume:" + record.report_id
    session._invoke.reset_mock()
    store.load_budget.return_value = TokenBudget(
        limit=1000, budget_id="budget", unknown_requests=1
    )
    with pytest.raises(ValueError, match="unknown"):
        await session._resume(record, None)
    session._invoke.assert_not_awaited()
