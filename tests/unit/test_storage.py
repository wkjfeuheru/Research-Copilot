from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tests.unit.test_orchestration import context


def _brief(task_id, role="searcher"):
    from research_copilot.contracts import TaskBrief

    return TaskBrief(task_id=task_id, role=role, objective=task_id, as_of="2025-04-30")


@pytest.mark.asyncio
async def test_production_lifecycle_rejects_non_postgres_before_opening_connections():
    from research_copilot.runtime.storage import postgres_runtime

    with pytest.raises(ValueError, match="postgresql"):
        async with postgres_runtime("sqlite://"):
            pytest.fail("A non-PostgreSQL production runtime opened")


@pytest.mark.asyncio
async def test_sql_store_survives_reconstruction_and_keeps_cancellation_sticky(
    tmp_path,
):
    from research_copilot.runtime.storage import SqlSessionStore
    from research_copilot.runtime.session import SessionRecord
    from research_copilot.runtime.budget import TokenBudget

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.sqlite'}")
    try:
        store = SqlSessionStore(engine)
        await store.setup()
        record = SessionRecord("report-1", "actor-1", "v2", "policy-1", "budget-1")
        from research_copilot.runtime.business import BusinessCoordinator

        store.resume_validator = BusinessCoordinator(store).validate_resume
        await store.create(
            record, TokenBudget(limit=1000, budget_id="budget-1").snapshot()
        )
        await store.request_cancel("report-1", "actor-1")
        await store.save(replace(record, status="running"))
        restarted = SqlSessionStore(engine)
        assert (await restarted.load("report-1", "actor-1")).cancel_requested
        with pytest.raises(LookupError):
            await restarted.load("report-1", "wrong-actor")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_business_approval_and_budget_reservation_are_durable(tmp_path):
    from research_copilot.runtime.storage import SqlSessionStore
    from research_copilot.runtime.session import SessionRecord
    from research_copilot.runtime.business import (
        BusinessCoordinator,
        ResearchPlan,
        approval_key,
    )
    from research_copilot.runtime.budget import TokenBudget

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.sqlite'}")
    try:
        store = SqlSessionStore(engine)
        await store.setup()
        record = SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        )
        await store.create(
            record, TokenBudget(limit=1000, budget_id="budget-1").snapshot()
        )
        business = BusinessCoordinator(store)
        artifact = await business.propose_plan(
            context(),
            ResearchPlan(
                report_type="company_report",
                objective="test",
                tasks=[_brief("collect")],
            ),
        )
        pending = {
            "i": {
                "action_requests": [
                    {
                        "name": "confirm_plan",
                        "args": {"artifact": artifact.model_dump()},
                    }
                ]
            }
        }
        store.resume_validator = business.validate_resume
        await store.validate_resume(
            record, "actor-1", pending, {"i": {"decisions": [{"type": "approve"}]}}
        )
        receipt = await business.confirm(
            context(),
            "confirm_plan",
            artifact,
            approval_key("report-1", "confirm_plan", artifact),
        )
        budget = await store.load_budget(context())
        reservation = await budget.reserve(600)
        restarted = SqlSessionStore(engine)
        restored_budget = await restarted.load_budget(context())
        assert restored_budget.snapshot()["pending"] == {reservation: 600}
        from research_copilot.runtime.budget import BudgetExceeded

        with pytest.raises(BudgetExceeded):
            await restored_budget.reserve(401)
        repeated = await BusinessCoordinator(restarted).confirm(
            context(),
            "confirm_plan",
            artifact,
            approval_key("report-1", "confirm_plan", artifact),
        )
        assert receipt == repeated
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_session_clarifies_and_recovers_with_sql_state(tmp_path):
    from research_copilot.runtime.storage import SqlSessionStore
    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.runtime.session import SessionRecord
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from research_copilot.runtime.session import SessionService
    from tests.unit.test_orchestration import ScriptModel, assemble, call

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native.sqlite'}")
    try:
        store = SqlSessionStore(engine)
        await store.setup()
        from research_copilot.runtime.business import BusinessCoordinator

        store.resume_validator = BusinessCoordinator(store).validate_resume
        record = SessionRecord("report-1", "actor-1", "v2", "policy-1", "budget-1")
        await store.create(
            record, TokenBudget(limit=1000000, budget_id="budget-1").snapshot()
        )
        model = ScriptModel(
            script=[
                call("ask_user", {"question": "Which company?"}),
                AIMessage(content="Need evidence"),
            ]
        )
        graph = assemble(
            tmp_path, model, checkpointer=InMemorySaver(), autonomous_workflow=True
        )

        def factory(record, control):
            return graph

        service = SessionService(
            store=store, agent_factory=factory, policy_version="policy-1"
        )
        first = await service.start("report-1", "actor-1", "research")
        assert first.status == "paused"
        restarted = SessionService(
            store=store, agent_factory=factory, policy_version="policy-1"
        )
        second = await restarted.resume(
            "report-1", "actor-1", {first.interrupts[0]["id"]: {"answer": "Company A"}}
        )
        assert second.status == "failed", "Plain model text cannot sign a report"
        assert model.position == 2
    finally:
        await engine.dispose()
