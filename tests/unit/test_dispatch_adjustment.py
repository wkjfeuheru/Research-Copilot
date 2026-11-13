import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.runtime.autonomy import DISPATCH_LIMIT, admit, progress
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.dispatch_adjustment import extend_dispatches
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.subagents.contracts import BusinessInputError
from tests.unit.test_orchestration import context


@pytest.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dispatch.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        TokenBudget(limit=100_000, budget_id="budget-1").snapshot(),
    )
    try:
        yield store
    finally:
        await engine.dispose()


def _brief(ident):
    return TaskBrief(
        task_id=ident,
        role="searcher",
        objective="Research company",
        as_of="2026-11-11",
    )


def _seed(store):
    def operation(state):
        from research_copilot.runtime.autonomy import freeze_scope

        state.role_data["workflow_version"] = "products-v2"
        freeze_scope(
            state,
            "report-1",
            {
                "report_type": "company_report",
                "objective": "Research company",
                "as_of": "2026-11-11",
            },
        )
        for index in range(DISPATCH_LIMIT):
            state.dispatches[f"d{index}"] = {
                "role": "searcher",
                "description": f"task {index}",
                "inputs": {},
            }

    return store.mutate_control(context(), operation)


async def test_thirteenth_dispatch_is_rejected_until_an_audited_extension(store):
    await _seed(store)

    def thirteenth(state):
        admit(
            state,
            Dispatch("d12", "searcher", "/workspace/tasks/d12/"),
            _brief("d12"),
            "task 12",
        )

    with pytest.raises(BusinessInputError, match="dispatch limit exhausted"):
        await store.mutate_control(context(), thirteenth)
    receipt = await extend_dispatches(
        store,
        report_id="report-1",
        actor_id="actor-1",
        additional=3,
        note="acceptance run needs analysis, writer and reviewer dispatches",
    )
    assert receipt["previous_limit"] == DISPATCH_LIMIT
    assert receipt["additional"] == 3
    state = await store.read_control(context())
    assert state.role_data["dispatch_extensions"][-1]["receipt_id"] == receipt[
        "receipt_id"
    ]
    assert progress(state)["dispatch_remaining"] == 3
    await store.mutate_control(context(), thirteenth)
    state = await store.read_control(context())
    assert "d12" in state.dispatches


async def test_second_extension_stacks_and_lower_request_is_rejected(store):
    await _seed(store)
    await extend_dispatches(
        store,
        report_id="report-1",
        actor_id="actor-1",
        additional=2,
        note="first operator grant",
    )
    with pytest.raises(ValueError, match="only be extended"):
        await extend_dispatches(
            store,
            report_id="report-1",
            actor_id="actor-1",
            additional=1,
            note="the unused quota already covers one dispatch",
        )
    # 消耗一个派单后，新一轮授权仍须严格高于既有授权允许的上限。
    await store.mutate_control(
        context(),
        lambda state: admit(
            state,
            Dispatch("d12", "searcher", "/workspace/tasks/d12/"),
            _brief("d12"),
            "task 12",
        ),
    )
    await extend_dispatches(
        store,
        report_id="report-1",
        actor_id="actor-1",
        additional=2,
        note="second operator grant",
    )
    state = await store.read_control(context())
    grants = state.role_data["dispatch_extensions"]
    assert [g["additional"] for g in grants] == [2, 2]
    assert grants[1]["previous_limit"] == DISPATCH_LIMIT + 2


async def test_extension_requires_a_reason(store):
    with pytest.raises(ValueError, match="note"):
        await extend_dispatches(
            store,
            report_id="report-1",
            actor_id="actor-1",
            additional=1,
            note="   ",
        )
    state = await store.read_control(context())
    assert not state.role_data.get("dispatch_extensions")
