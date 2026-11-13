"""Business admission for native planning: no synthetic approved plan."""

import asyncio
import pytest

from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.subagents.services import RoleServices
from tests.unit.test_business import ControlRepository
from tests.unit.test_orchestration import context


async def setup(tmp_path):
    repository = ControlRepository()
    repository.state.role_data["workflow_version"] = "products-v2"
    business = BusinessCoordinator(repository)
    services = RoleServices(
        repository=repository, business=business, workspace_root=tmp_path
    )
    assert hasattr(business, "set_report_scope"), (
        "native planning needs a frozen report scope"
    )
    await business.set_report_scope(
        context(),
        {
            "report_type": "company_report",
            "objective": "Research company",
            "as_of": "2025-04-30",
        },
    )
    return repository, business, services


def brief(ident, **updates):
    return TaskBrief(
        task_id=ident,
        role="searcher",
        objective="Research company",
        as_of="2025-04-30",
        **updates,
    )


async def admit(services, ident, task=None):
    return await services.prepare_dispatch(
        context(),
        Dispatch(ident, "searcher", f"/workspace/tasks/{ident}/"),
        (task or brief(ident)).model_dump_json(),
    )


@pytest.mark.asyncio
async def test_derived_collection_fields_are_rejected_before_dispatch(tmp_path):
    repo, _, services = await setup(tmp_path)
    with pytest.raises(ValueError, match="gross_profit"):
        await admit(
            services,
            "derived",
            brief("derived", required_fields=["gross_profit_2026Q3"]),
        )
    assert not repo.state.dispatches
    assert not repo.state.role_data.get("tasks")


@pytest.mark.asyncio
async def test_scope_is_frozen_without_plan_or_human_approval(tmp_path):
    repo, business, services = await setup(tmp_path)
    await admit(services, "d1")
    assert not repo.state.plans and not repo.state.approved
    assert repo.state.role_data["research_as_of"] == brief("d1").as_of
    with pytest.raises(ValueError, match="frozen"):
        await business.set_report_scope(
            context(),
            {
                "report_type": "company_report",
                "objective": "Different",
                "as_of": "2025-04-30",
            },
        )


@pytest.mark.asyncio
async def test_atomic_cap_and_replay(tmp_path):
    repo, _, services = await setup(tmp_path)
    results = await asyncio.gather(
        *(admit(services, f"d{i}") for i in range(13)), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in results) == 12
    assert len(repo.state.dispatches) == 12
    await admit(services, "d0")
    assert len(repo.state.dispatches) == 12
    with pytest.raises(ValueError, match="changed"):
        await admit(services, "d0", brief("different"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"as_of": "2024-01-01"}, {"input_refs": ["unknown"]}, {"max_facts": 16}]
)
async def test_rejected_contract_consumes_no_dispatch(tmp_path, changes):
    repo, _, services = await setup(tmp_path)
    task = brief("invalid").model_copy(update=changes)
    with pytest.raises(ValueError):
        await admit(services, "invalid", task)
    assert not repo.state.dispatches
    assert not repo.state.role_data.get("tasks")


@pytest.mark.asyncio
async def test_stage_policy_change_does_not_block_resume_but_model_change_does(
    tmp_path,
):
    repo, _, services = await setup(tmp_path)
    frozen = {
        "preflight": {"mode": "production", "execution_policy": {"tokens": 1800000}},
        "models": {"supervisor": {"model": "deepseek-chat"}},
    }
    await repo.mutate_control(
        context(), lambda state: state.role_data.update(runtime_manifest=frozen)
    )
    services.runtime_manifest = {
        "preflight": {"mode": "production", "execution_policy": {"tokens": 2400000}},
        "models": {"supervisor": {"model": "deepseek-chat"}},
    }
    # The budget ledger is frozen per report; a later policy-file cap change
    # affects only new reports and must not break an in-flight resume.
    await admit(services, "after-policy-change")

    services.runtime_manifest["models"]["supervisor"]["model"] = "different-model"
    with pytest.raises(ValueError, match="Runtime configuration changed"):
        await admit(services, "after-model-change")
    assert "after-model-change" not in repo.state.dispatches


@pytest.mark.asyncio
async def test_work_group_does_not_change_with_task_identity(tmp_path):
    from research_copilot.sandbox.execution_policy import work_key

    repo, _, services = await setup(tmp_path)
    await admit(services, "first")
    await admit(services, "renamed")
    state = repo.state
    assert work_key("first", state.role_data["scopes"], state.dispatches) == work_key(
        "renamed", state.role_data["scopes"], state.dispatches
    )
    assert state.dispatches["first"].get("work_group"), (
        "work group must be server owned"
    )


@pytest.mark.asyncio
async def test_chart_task_scope_keeps_repair_purpose(tmp_path):
    """图表任务 bind 后 scope 必须保留补齐的修复用途（WLY-20261108 第八轮）。"""
    repo, _, services = await setup(tmp_path)
    task = TaskBrief(
        task_id="chart",
        role="analysist",
        objective="生成图表",
        as_of="2025-04-30",
        calculation_mode="custom",
        execution_purposes=["register_metrics", "render_chart"],
        max_executions=2,
    )
    await services.prepare_dispatch(
        context(),
        Dispatch("chart", "analysist", "/workspace/tasks/chart/"),
        task.model_dump_json(),
    )
    scope = repo.state.role_data["scopes"]["chart"]
    assert scope["analysis_execution_limit"] >= 3
    assert scope["execution_purposes"] == [
        "register_metrics",
        "render_chart",
        "render_chart_repair",
    ]


def test_autonomous_tools_and_context_have_no_plan_gate():
    from research_copilot.orchestration.tools import build_top_tools
    from research_copilot.orchestration.context import supervisor_index

    repo = ControlRepository()
    repo.state.role_data["workflow_version"] = "products-v2"
    tools = build_top_tools(BusinessCoordinator(repo), autonomous=True)
    names = {t.name for t in tools}
    assert {"set_report_scope", "ask_user", "signoff_report"} <= names
    assert not names & {
        "propose_plan",
        "confirm_plan",
        "confirm_analysis",
        "confirm_rework",
    }
    assert "next_action" not in supervisor_index(repo.state)


def test_supervisor_sees_shared_collection_limit_and_remaining_calls():
    from research_copilot.orchestration.context import supervisor_index

    repo = ControlRepository()
    repo.state.role_data.update(
        workflow_version="products-v2",
        collection_work={"group": {"calls": {"a": "x", "b": "y"},
                                  "max_source_calls": 2, "max_no_progress_calls": 2,
                                  "no_progress_calls": 0}},
    )
    budget = supervisor_index(repo.state)["collection_budgets"]["group"]
    assert budget["used_calls"] == 2
    assert budget["remaining_calls"] == 0
    assert budget["scope"] == "shared_work_group"
    assert repo.state.role_data["collection_work"]["group"]["max_source_calls"] == 2


def test_clarification_payload_requires_nonempty_answer():
    from research_copilot.runtime.session import validate_decisions

    pending = {"q": {"kind": "clarification", "question": "Which company?"}}
    assert validate_decisions(pending, {"q": {"answer": "Company A"}})
    for value in (
        {"answer": " "},
        {"decisions": [{"type": "approve"}]},
        {"answer": "A", "approve": True},
    ):
        with pytest.raises(ValueError):
            validate_decisions(pending, {"q": value})


@pytest.mark.asyncio
async def test_role_scope_uses_frozen_scope_not_approved_plan(tmp_path):
    from research_copilot.runtime.control import active_dispatch

    repo, _, services = await setup(tmp_path)
    await admit(services, "s")
    dispatch = Dispatch("s", "searcher", "/workspace/tasks/s/")
    token = active_dispatch.set(dispatch)
    try:
        assert (await services.scope(context()))[0] == dispatch
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_rework_has_no_approval_gate_but_keeps_issue_validation():
    from tests.unit.test_rework import setup_rework
    from research_copilot.runtime.rework import status, validate_dispatch

    repo, business = setup_rework()
    repo.state.role_data["workflow_version"] = "products-v2"
    repo.state.artifacts["scope"] = repo.state.artifacts.pop("plan")
    repo.state.approved.clear()
    ref = await business.propose_rework(
        context(), "r", [{"role": "analysist", "objective": "fix", "issue_ids": ["i"]}]
    )
    assert status(repo.state)["phase"] == "in_progress"
    assert "rework" not in repo.state.approved
    validate_dispatch(
        repo.state, {"role": "analysist", "inputs": {"rework": ref.model_dump()}}
    )
    with pytest.raises(ValueError, match="issue"):
        await business.propose_rework(
            context(),
            "r",
            [{"role": "writer", "objective": "fix", "issue_ids": ["fake"]}],
        )


def test_signoff_uses_valid_analysis_without_claiming_human_approval():
    from tests.unit.test_rework import setup_rework
    from research_copilot.runtime.signoff import signoff_error

    repo, _ = setup_rework()
    s = repo.state
    s.role_data["workflow_version"] = "products-v2"
    s.artifacts["scope"] = s.artifacts.pop("plan")
    s.approved.clear()
    s.reviewed_report = s.artifacts["report"]
    s.role_data["reviews"]["r"].update(
        outcome="passed", checks_complete=True, pass_confidence=0.95
    )
    s.role_data["reviews"]["r"]["issues"] = []
    s.role_data["objects"] = {
        "report": {
            "ref": s.artifacts["report"].model_dump(),
            "payload": {"analysis_ref": s.artifacts["analysis"].model_dump()},
        }
    }
    assert signoff_error(s, {"usage_verified": True}) is None
    s.artifacts["analysis"] = s.artifacts["analysis"].model_copy(
        update={"version": "2"}
    )
    assert "stale" in signoff_error(s, {"usage_verified": True})


@pytest.mark.asyncio
async def test_repair_requires_known_failure_and_is_bounded(tmp_path):
    repo, _, services = await setup(tmp_path)
    await admit(services, "original")
    repo.state.role_data["dispatch_failures"] = {
        "original": {"repairable": True, "code": "invalid_task_input"}
    }
    # A new task ID must not silently evade a recorded failure.
    with pytest.raises(ValueError, match="repair"):
        await admit(services, "renamed")
    repaired = brief("repair").model_copy(update={"repair_of": "original"})
    with pytest.raises(ValueError, match="allowances"):
        await admit(
            services, "repair", repaired.model_copy(update={"max_source_calls": 9})
        )
    await admit(services, "repair", repaired)
    assert repo.state.role_data["scopes"]["repair"]["correction_of"] == "original"
    repo.state.role_data["dispatch_failures"]["repair"] = {
        "repairable": True,
        "code": "invalid_task_input",
    }
    with pytest.raises(ValueError, match="repair"):
        await admit(
            services, "again", brief("again").model_copy(update={"repair_of": "repair"})
        )


@pytest.mark.asyncio
async def test_unknown_execution_blocks_new_dispatch(tmp_path):
    repo, _, services = await setup(tmp_path)
    repo.state.role_data["executions"] = {"e": {"status": "unknown"}}
    with pytest.raises(ValueError, match="unknown"):
        await admit(services, "new")
    assert not repo.state.dispatches


@pytest.mark.asyncio
async def test_analysis_can_submit_without_plan_and_revokes_old_review(tmp_path):
    from research_copilot.runtime.control import active_dispatch

    repo, _, services = await setup(tmp_path)
    # This fixture submits a qualitative artifact without quantitative metrics.
    task = brief("analysis").model_copy(
        update={"role": "analysist", "calculation_mode": "custom"}
    )
    dispatch = Dispatch("analysis", "analysist", "/workspace/tasks/analysis/")
    await services.prepare_dispatch(context(), dispatch, task.model_dump_json())
    token = active_dispatch.set(dispatch)
    try:
        receipt = await services.publish_object(
            context(), "submit", "analysis", {"summary": "verified"}
        )
        assert receipt["ok"]
        assert "analysis" in repo.state.artifacts
        assert not repo.state.approved
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_source_quota_survives_renamed_task_and_replay(tmp_path):
    from research_copilot.subagents.collection import (
        begin_source_call,
        finish_source_call,
    )

    repo, _, services = await setup(tmp_path)
    await admit(services, "first", brief("first", max_source_calls=1))
    first = Dispatch("first", "searcher", "/workspace/tasks/first/")
    call = {"id": "call-1", "name": "web_search", "args": {"query": "test"}}
    assert await begin_source_call(services, context(), first, call) is None
    assert await begin_source_call(services, context(), first, call) is None
    await finish_source_call(services, context(), first, call)
    await admit(services, "renamed")
    renamed = Dispatch("renamed", "searcher", "/workspace/tasks/renamed/")
    assert (
        await begin_source_call(services, context(), renamed, {**call, "id": "call-2"})
        == "source_call_limit"
    )


@pytest.mark.asyncio
async def test_stale_projection_path_is_not_an_input_reference(tmp_path):
    repo, _, services = await setup(tmp_path)
    await admit(services, "first")
    repo.state.role_data["scopes"]["first"]["paths"][
        "/workspace/artifacts/first/old-analysis.json"
    ] = "a" * 64
    with pytest.raises(ValueError, match="stale"):
        await admit(
            services,
            "next",
            brief("next", input_refs=["/workspace/artifacts/first/old-analysis.json"]),
        )


@pytest.mark.asyncio
async def test_terminal_role_failure_stops_parent_model(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from research_copilot.orchestration.middleware import DispatchMiddleware
    from research_copilot.runtime.control import RunControl

    repo, business, services = await setup(tmp_path)
    await admit(services, "failed")
    repo.state.role_data["dispatch_failures"] = {
        "failed": {"repairable": False, "code": "stage_budget_exhausted"}
    }
    handler = AsyncMock()
    request = SimpleNamespace(runtime=SimpleNamespace(context=context()))
    with pytest.raises(RuntimeError, match="recovery"):
        await DispatchMiddleware(
            business, RunControl(), context().report_id
        ).awrap_model_call(request, handler)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupted_attempt_is_not_projected_as_running(tmp_path):
    from research_copilot.runtime.autonomy import progress

    repo, _, services = await setup(tmp_path)
    await admit(services, "interrupted")
    repo.state.role_data["last_run_error"] = {"code": "ExecutionUnknown"}
    assert progress(repo.state)["tasks"][0]["status"] == "failed"
