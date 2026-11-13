"""Evaluate blocked collection through real authorization/result persistence."""

from copy import deepcopy
import json

import pytest

from research_copilot.runtime.business import ResearchPlan
from research_copilot.runtime.run_evaluation import evaluate_runs
from tests.integration.conftest import plan_tasks
from tests.integration.test_run_evaluation import bindings, suite
from tests.integration.test_searcher import dispatch
from tests.unit.test_orchestration import context


async def collected(env, *, accept=True, conflict=False, ident="collect-1"):
    services, business, store = env
    tasks = plan_tasks()
    tasks[0] = tasks[0].model_copy(update={"required_fields": ["revenue_2024"]})
    if ident == "collect-1":
        plan = await business.propose_plan(
            context(),
            ResearchPlan(
                report_type="company_report",
                objective="Frozen missing/conflict fixture",
                tasks=tasks,
            ),
        )
        # Establish the fixture's prior human confirmation, never a real approval.
        await store.mutate_control(
            context(), lambda state: state.approved.update(plan=plan)
        )
    scope = await dispatch(
        env, ident=ident, description=json.dumps(tasks[0].model_dump())
    )
    raw = {
        "task_id": "collect",
        "status": "blocked",
        "summary": "Evidence cannot support the required field",
        "gaps": [
            {
                "description": "revenue_2024 unavailable",
                "impact": "Cannot compute growth",
                "suggested_role": "searcher",
            }
        ],
        "source_warnings": ["conflicting_sources"] if conflict else [],
        "stop_reason": "blocked",
    }
    mapped = await services.map_result(context(), scope, raw)
    if accept:
        await business.accept_result(context(), scope, mapped)
    return scope


def missing_suite():
    spec = suite()
    spec["cases"][0].update(
        scenario="missing_evidence",
        checks=[
            {
                "id": "missing",
                "root": "collection",
                "path": ["collect", "missing_required"],
                "match": "contains",
                "expected": "revenue_2024",
            },
            {
                "id": "gap",
                "root": "collection",
                "path": ["collect", "gaps"],
                "match": "contains",
                "expected": {
                    "description": "revenue_2024 unavailable",
                    "suggested_role": "searcher",
                },
            },
            {
                "id": "no-facts",
                "root": "collection",
                "path": ["collect", "facts"],
                "expected": [],
            },
            {
                "id": "blocked",
                "root": "collection",
                "path": ["collect", "accepted_status"],
                "expected": "needs_input",
            },
        ],
    )
    return spec


@pytest.mark.parametrize("conflict", [False, True])
async def test_collection_gap_without_analysis_is_quality_success_not_completion(
    role_env, conflict
):
    await collected(role_env, conflict=conflict)
    store = role_env[2]
    before = await store.read_snapshot("report-1", "actor-1")
    assert "analysis" not in before.state.artifacts
    assert not before.state.role_data.get("fact_sheets")
    spec = missing_suite()
    if conflict:
        spec["cases"][0]["scenario"] = "conflicting_sources"
        spec["cases"][0]["checks"].append(
            {
                "id": "conflict",
                "root": "collection",
                "path": ["collect", "source_warnings"],
                "match": "contains",
                "expected": "conflicting_sources",
            }
        )
    result = await evaluate_runs(store, "actor-1", spec, bindings())
    assert result["cases"][0]["quality_passed"]
    assert result["budget_curve"][0]["agent_completion_rate"] == 0
    after = await store.read_snapshot("report-1", "actor-1")
    assert before.state == after.state
    assert before.budget.snapshot() == after.budget.snapshot()


@pytest.mark.parametrize(
    "change",
    [
        "unaccepted",
        "old_plan",
        "revoked",
        "wrong_task",
        "scope",
        "parallel",
        "rework",
        "changed_result",
    ],
)
async def test_stale_unaccepted_or_ambiguous_collection_cannot_satisfy_truth(
    role_env, change
):
    scope = await collected(role_env, accept=change != "unaccepted")
    store = role_env[2]
    if change == "parallel":
        await collected(role_env, accept=False, ident="parallel")
    else:

        def alter(state):
            if change == "old_plan":
                state.dispatches[scope.dispatch_id]["inputs"]["plan"]["version"] = "old"
            elif change == "revoked":
                state.approved.pop("plan")
            elif change == "wrong_task":
                state.role_data["task_results"][scope.dispatch_id]["task_id"] = "other"
            elif change == "scope":
                state.role_data["scopes"][scope.dispatch_id]["task_brief"][
                    "max_source_calls"
                ] = 999
            elif change == "rework":
                state.artifacts["rework"] = state.artifacts["plan"]
            elif change == "changed_result":
                state.results[scope.dispatch_id]["gaps"] = ["different accepted result"]

        await store.mutate_control(context(), alter)
    result = await evaluate_runs(store, "actor-1", missing_suite(), bindings())
    assert not result["cases"][0]["quality_passed"]
    assert not any(check["passed"] for check in result["cases"][0]["checks"])


async def test_explicit_correction_excludes_old_attempt_even_before_target_result(
    role_env,
):
    original = await collected(role_env)
    target = await collected(role_env, accept=False, ident="correction")
    services, business, store = role_env

    def authorize(state):
        state.role_data["dispatch_recoveries"] = {
            "call-1": {
                "original_dispatch_id": original.dispatch_id,
                "target_dispatch_id": target.dispatch_id,
                "authorization_id": "fixture-authorization",
                "plan": state.artifacts["plan"].model_dump(),
            }
        }
        state.role_data["scopes"][target.dispatch_id]["correction_of"] = (
            original.dispatch_id
        )

    await store.mutate_control(context(), authorize)
    result = await evaluate_runs(store, "actor-1", missing_suite(), bindings())
    assert not result["cases"][0]["quality_passed"]
    state = await store.read_control(context())
    raw = deepcopy(state.role_data["task_results"][target.dispatch_id])
    mapped = await services.map_result(context(), target, raw)
    await business.accept_result(context(), target, mapped)
    result = await evaluate_runs(store, "actor-1", missing_suite(), bindings())
    assert result["cases"][0]["quality_passed"]


@pytest.mark.parametrize("tamper_source", [False, True])
async def test_collection_fact_truth_retains_source_hash_and_period_before_analysis(
    role_env, tamper_source
):
    from research_copilot.subagents.services import digest

    scope = await collected(role_env, accept=False)
    services, business, store = role_env
    raw = {"title": "Synthetic annual report", "body": "Revenue 120"}
    content_hash = digest(raw)

    def source(state):
        state.role_data.setdefault("evidence", {})["E-1"] = {
            "kind": "original_text",
            "raw": raw,
            "content_hash": content_hash,
            "source": {"server": "test", "tool": "frozen", "arguments": {}},
            "collected_at": "2025-04-30T00:00:00+00:00",
        }
        state.role_data["scopes"][scope.dispatch_id]["evidence_ids"] = ["E-1"]

    await store.mutate_control(context(), source)
    value = {
        "task_id": "collect",
        "status": "completed",
        "summary": "Required field collected",
        "evidence_ids": ["E-1"],
        "stop_reason": "required_complete",
        "facts": [
            {
                "key": "revenue_2024",
                "evidence_id": "E-1",
                "content_hash": content_hash,
                "pointer": "/body",
                "quote": "Revenue 120",
                "raw_value": "120",
                "unit": "元",
                "currency": "CNY",
                "period": "2024",
                "entity": "AAA",
                "basis": "合并",
            }
        ],
    }
    mapped = await services.map_result(context(), scope, value)
    await business.accept_result(context(), scope, mapped)
    if tamper_source:
        await store.mutate_control(
            context(),
            lambda state: state.role_data["evidence"]["E-1"]["raw"].update(
                body="Revenue 999"
            ),
        )
    spec = suite()
    spec["cases"][0]["checks"] = [
        {
            "id": "fact",
            "root": "collection",
            "path": ["collect", "facts"],
            "match": "contains",
            "expected": {
                "input_ref": {
                    "key": "revenue_2024",
                    "raw_value": "120",
                    "period": "2024",
                    "content_hash": content_hash,
                }
            },
        }
    ]
    result = await evaluate_runs(store, "actor-1", spec, bindings())
    assert result["cases"][0]["quality_passed"] is not tamper_source
