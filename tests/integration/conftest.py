"""Shared integration fixture; SQLite only, no live model or source calls."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from research_copilot.contracts import TaskBrief
from research_copilot.runtime.business import BusinessCoordinator, ResearchPlan
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore
from tests.unit.test_orchestration import context


def plan_tasks():
    return [
        TaskBrief(
            task_id="collect", role="searcher", objective="collect", as_of="2025-04-30"
        ),
        TaskBrief(
            task_id="analysis",
            role="analysist",
            objective="analyze",
            as_of="2025-04-30",
        ),
        TaskBrief(
            task_id="write", role="writer", objective="write", as_of="2025-04-30"
        ),
        TaskBrief(
            task_id="review", role="reviewer", objective="review", as_of="2025-04-30"
        ),
    ]


@pytest.fixture
async def role_env(tmp_path):
    from research_copilot.subagents.services import RoleServices

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'roles.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    await store.create(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        TokenBudget(limit=1000000, budget_id="budget-1").snapshot(),
        workflow_version="products-v1",  # Legacy role-service contracts, without session execution.
    )
    business = BusinessCoordinator(store)
    plan = await business.propose_plan(
        context(),
        ResearchPlan(
            report_type="company_report",
            objective="research",
            tasks=plan_tasks(),
        ),
    )
    # Fixture establishes a previously human-confirmed plan; no UI approval is automated.
    await store.mutate_control(
        context(), lambda state: state.approved.update(plan=plan)
    )
    services = RoleServices(
        repository=store,
        workspace_root=tmp_path / "workspace",
        business=business,
        report_rules={
            "version": 1,
            "sensitive_words": [],
            "prohibited_patterns": [],
            "required_disclosures": ["risk_warning", "disclaimer"],
            "risk_warning_template": "Data and methodology risks apply.",
            "disclaimer_template": "For research reference only.",
            "is_example": False,
        },
    )
    yield services, business, store
    await engine.dispose()
