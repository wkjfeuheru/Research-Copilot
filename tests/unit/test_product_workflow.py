import pytest
from research_copilot.runtime.business import ControlState
from research_copilot.contracts import ArtifactRef
from research_copilot.runtime.workflow import (
    decide_next,
    validate_product_plan,
    WorkflowBlocked,
)


def ref(name):
    return ArtifactRef(artifact_id=name, version="1", content_hash="a" * 64)


def state():
    p = ref("plan")
    tasks = [
        {"task_id": r, "role": r, "objective": r, "as_of": "2025-04-30T00:00:00+08:00"}
        for r in ["searcher", "analysist", "writer", "reviewer"]
    ]
    return ControlState(
        artifacts={"plan": p},
        approved={"plan": p},
        plans=[{"artifact": p.model_dump(), "plan": {"tasks": tasks}}],
    )


def finish(s, action, status="completed"):
    from research_copilot.runtime.workflow import dispatch_id

    s.results[dispatch_id("r", action["id"])] = {"status": status}


def test_normal_route_gates_analysis_and_signoff_without_planner():
    s = state()
    a = decide_next(s, "r")
    assert a["args"]["subagent_type"] == "searcher"
    assert a == decide_next(s, "r")
    finish(s, a)
    a = decide_next(s, "r")
    assert a["args"]["subagent_type"] == "analysist"
    finish(s, a)
    s.artifacts["analysis"] = ref("a")
    assert decide_next(s, "r")["name"] == "confirm_analysis"
    s.approved["analysis"] = s.artifacts["analysis"]
    a = decide_next(s, "r")
    assert a["args"]["subagent_type"] == "writer"
    finish(s, a)
    s.artifacts["report"] = ref("report")
    a = decide_next(s, "r")
    assert a["args"]["subagent_type"] == "reviewer"
    finish(s, a, "passed")
    s.reviewed_report = s.artifacts["report"]
    assert decide_next(s, "r")["name"] == "signoff_report"
    s.approved["report"] = s.artifacts["report"]
    assert decide_next(s, "r")["kind"] == "complete"


def test_plan_rejects_derived_values_as_collection_required_fields():
    tasks = state().plans[0]["plan"]["tasks"]
    tasks[0]["required_fields"] = [
        "revenue_2026Q3",
        "gross_profit_2026Q3",
        "gross_margin_q3_2026",
        "revenue_qoq_growth_q3_2026",
        "revenue_change_q3_2026",
    ]
    with pytest.raises(ValueError, match="gross_profit"):
        validate_product_plan(tasks)
    tasks[0]["required_fields"] = [
        "revenue_2026Q1",
        "cost_of_sales_2026Q1",
        "revenue_2026Q2",
        "cost_of_sales_2026Q2",
        "change_in_inventories_q3_2026",
        "margin_trading_balance_2026Q3",
    ]
    assert validate_product_plan(tasks)


def test_plan_rejects_overlapping_searcher_fields():
    tasks = [
        {
            "task_id": "s1",
            "role": "searcher",
            "objective": "a",
            "as_of": "2025-04-30T00:00:00+08:00",
            "required_fields": ["revenue_2024"],
        },
        {
            "task_id": "s2",
            "role": "searcher",
            "objective": "b",
            "as_of": "2025-04-30T00:00:00+08:00",
            "required_fields": ["revenue_2024"],
        },
        *[
            {
                "task_id": role,
                "role": role,
                "objective": role,
                "as_of": "2025-04-30T00:00:00+08:00",
            }
            for role in ("analysist", "writer", "reviewer")
        ],
    ]
    with pytest.raises(ValueError, match="same field"):
        validate_product_plan(tasks)


def test_partial_analysis_with_saved_artifact_reaches_confirmation_gate():
    from research_copilot.runtime.workflow import dispatch_id

    s = state()
    finish(s, decide_next(s, "r"))
    analysis_action = decide_next(s, "r")
    finish(s, analysis_action, "needs_input")
    s.role_data["task_results"] = {
        dispatch_id("r", analysis_action["id"]): {"status": "partial"}
    }
    with pytest.raises(WorkflowBlocked):
        decide_next(s, "r")
    s.artifacts["analysis"] = ref("analysis")
    assert decide_next(s, "r")["name"] == "confirm_analysis"


def test_incomplete_task_does_not_redispatch():
    s = state()
    finish(s, decide_next(s, "r"), "needs_input")
    with pytest.raises(WorkflowBlocked):
        decide_next(s, "r")


def test_chained_authorized_recoveries_are_followed():
    from research_copilot.runtime.workflow import command

    s = state()
    original = decide_next(s, "r")
    s.role_data["dispatch_recoveries"] = {
        original["id"]: {"authorization_id": "auth-1"}
    }
    second = command("task", {}, [original["id"], "auth-1"])["id"]
    s.role_data["dispatch_recoveries"][second] = {"authorization_id": "auth-2"}
    third = command("task", {}, [second, "auth-2"])["id"]
    assert decide_next(s, "r")["id"] == third


def test_authorized_recovery_changes_only_failed_dispatch_identity():
    s = state()
    original = decide_next(s, "r")
    s.role_data["dispatch_recoveries"] = {
        original["id"]: {"authorization_id": "user-budget-recovery-1"}
    }
    recovered = decide_next(s, "r")
    assert recovered["id"] != original["id"]
    assert recovered["args"] == original["args"]
    assert recovered == decide_next(s, "r")
    finish(s, recovered)
    assert decide_next(s, "r")["args"]["subagent_type"] == "analysist"


def test_missing_product_after_completed_task_is_blocked():
    s = state()
    finish(s, decide_next(s, "r"))
    finish(s, decide_next(s, "r"))
    with pytest.raises(WorkflowBlocked):
        decide_next(s, "r")


def test_product_plan_requires_each_role_unique_task_and_same_date():
    tasks = state().plans[0]["plan"]["tasks"]
    validate_product_plan(tasks)
    with pytest.raises(ValueError):
        validate_product_plan(tasks[:-1])
    with pytest.raises(ValueError):
        validate_product_plan(tasks + [tasks[0]])
    with pytest.raises(ValueError):
        validate_product_plan([dict(t, task_id="same") for t in tasks])


def test_writer_rework_preserves_approved_analysis_and_skips_collection():
    s = state()
    s.artifacts.update(analysis=ref("a"), report=ref("old"), rework=ref("rw"))
    s.approved.update(analysis=s.artifacts["analysis"], rework=s.artifacts["rework"])
    s.role_data["rework"] = {
        "rw": {
            "review_id": "review",
            "tasks": [{"role": "writer", "objective": "fix text", "issue_ids": ["i"]}],
        }
    }
    a = decide_next(s, "r")
    assert a["args"]["subagent_type"] == "writer"
    assert "fix text" in a["args"]["description"]
    assert s.artifacts["report"].artifact_id == "old"


def test_analysis_rework_needs_fresh_analysis_confirmation():
    s = state()
    s.artifacts.update(analysis=ref("old"), report=ref("old-report"), rework=ref("rw"))
    s.approved["rework"] = s.artifacts["rework"]
    s.role_data["rework"] = {
        "rw": {
            "review_id": "review",
            "tasks": [{"role": "analysist", "objective": "fix", "issue_ids": ["i"]}],
        }
    }
    a = decide_next(s, "r")
    assert a["args"]["subagent_type"] == "analysist"
    finish(s, a)
    s.artifacts["analysis"] = ref("new")
    assert decide_next(s, "r")["args"]["artifact"]["artifact_id"] == "new"


def test_incomplete_or_stale_review_cannot_sign():
    s = state()
    for role in ("searcher", "analysist", "writer"):
        if role == "writer":
            s.approved["analysis"] = s.artifacts["analysis"]
        finish(s, decide_next(s, "r"))
        if role == "analysist":
            s.artifacts["analysis"] = ref("a")
        if role == "writer":
            s.artifacts["report"] = ref("r")
    finish(s, decide_next(s, "r"), "passed")
    with pytest.raises(WorkflowBlocked):
        decide_next(s, "r")
