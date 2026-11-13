"""Atomic dispatch admission on SQLite and an explicitly isolated PostgreSQL DB."""

import asyncio
import pytest
from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord
from research_copilot.subagents.services import RoleServices
from tests.unit.test_orchestration import context
from tests.integration import test_full_product_flow as product_fixtures

from tests.integration import test_maintenance_postgres as maintenance_fixtures

fresh_product_environment = product_fixtures.fresh_product_environment

databases = maintenance_fixtures.databases


async def test_native_concurrent_admission_and_replay(
    fresh_product_environment, tmp_path
):
    store, _ = fresh_product_environment
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        TokenBudget(limit=100000, budget_id="budget-1").snapshot(),
    )
    business = BusinessCoordinator(store)
    services = RoleServices(
        repository=store, business=business, workspace_root=tmp_path / "workspace"
    )
    await business.set_report_scope(
        context(),
        {
            "objective": "company",
            "report_type": "company_report",
            "as_of": "2025-04-30",
        },
    )

    async def dispatch(index):
        ident = "task-" + str(index)
        task = TaskBrief(
            task_id=ident, role="searcher", objective="company", as_of="2025-04-30"
        )
        return await services.prepare_dispatch(
            context(),
            Dispatch(ident, "searcher", f"/workspace/tasks/{ident}/"),
            task.model_dump_json(),
        )

    results = await asyncio.gather(
        *(dispatch(i) for i in range(13)), return_exceptions=True
    )
    state = await store.read_control(context())
    assert len(state.dispatches) == len(state.role_data["tasks"]) == 12
    assert sum(isinstance(r, Exception) for r in results) == 1
    admitted = next(
        i for i, result in enumerate(results) if not isinstance(result, Exception)
    )
    await dispatch(admitted)
    assert len((await store.read_control(context())).dispatches) == 12
    assert not state.plans and not state.approved


async def test_invalid_contract_rolls_back_binding_and_count(
    fresh_product_environment, tmp_path
):
    store, _ = fresh_product_environment
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        TokenBudget(limit=100000, budget_id="budget-1").snapshot(),
    )
    business = BusinessCoordinator(store)
    services = RoleServices(
        repository=store, business=business, workspace_root=tmp_path / "workspace"
    )
    await business.set_report_scope(
        context(),
        {
            "objective": "company",
            "report_type": "company_report",
            "as_of": "2025-04-30",
        },
    )
    await store.mutate_control(
        context(), lambda s: s.role_data.update(runtime_manifest={"changed": True})
    )
    task = TaskBrief(
        task_id="bad", role="searcher", objective="company", as_of="2025-04-30"
    )
    with pytest.raises(ValueError, match="configuration changed"):
        await services.prepare_dispatch(
            context(),
            Dispatch("bad", "searcher", "/workspace/tasks/bad/"),
            task.model_dump_json(),
        )
    state = await store.read_control(context())
    assert not state.dispatches and not state.role_data.get("tasks")


@pytest.mark.db
async def test_history_server_starts_without_models_on_readonly_postgres(
    databases, tmp_path
):
    import httpx
    from types import SimpleNamespace
    from sqlalchemy import text
    from research_copilot.web.app import build_server_app
    from research_copilot.runtime.storage import postgres_runtime

    async with postgres_runtime(databases[0]) as (store, _):
        await store.create(
            SessionRecord("history", "actor-1", "v2", "policy-1", "history"),
            TokenBudget(limit=1000, budget_id="history").snapshot(),
            workflow_version="products-v1",
        )
    app = build_server_app(
        settings=SimpleNamespace(database_url=databases[0]),
        models_path=None,
        workspace_root=tmp_path / "history-workspace",
        actor_id="actor-1",
        policy_version="policy-1",
        token_limit=1000,
        mode="test",
        rules_path=tmp_path / "missing-rules",
        rules_approval_path=None,
        execution_policy_path=tmp_path / "missing-policy",
        static_dir=None,
        history_only=True,
    )
    async with app.router.lifespan_context(app):
        async with app.state.backend.store.engine.connect() as conn:
            assert (
                await conn.execute(text("SHOW transaction_read_only"))
            ).scalar_one() == "on"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            page = (await client.get("/api/reports/history")).json()
            assert page["read_only"] and not page["actions"]
            assert (
                await client.post("/api/reports", json={"user_request": "new"})
            ).status_code == 409
            assert (await client.get("/api/health")).json()["checks"]["model"][
                "status"
            ] != "ok"
