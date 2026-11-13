import importlib
import pytest


def test_new_budget_uses_policy_limit_unless_explicitly_narrowed():
    """缺省上限必须来自执行策略；显式值只能收紧（serve CLI 曾硬编码覆盖）。"""
    from research_copilot.runtime.preflight import load_execution_policy, new_budget

    policy = load_execution_policy()
    assert new_budget("default").snapshot()["limit"] == policy["token_limit"]
    narrowed = new_budget("narrow", token_limit=1000).snapshot()
    assert narrowed["limit"] == 1000


def test_work_item_execution_limit_survives_redispatch_and_unknown_blocks():
    module = importlib.import_module("research_copilot.sandbox.execution_policy")
    scopes = {
        "d1": {"task_brief": {"task_id": "a", "max_executions": 2}},
        "d2": {"task_brief": {"task_id": "a", "max_executions": 2}},
    }
    bindings = {k: {"inputs": {"plan": {"content_hash": "p"}}} for k in scopes}
    records = [
        {"dispatch_id": "d1", "status": "failed", "output": "syntax error"},
        {"dispatch_id": "d1", "status": "failed", "output": "syntax error"},
    ]
    with pytest.raises(RuntimeError, match="count"):
        module.check_execution_policy("d2", scopes, bindings, records, 20)
    with pytest.raises(RuntimeError, match="unknown"):
        module.check_execution_policy(
            "d2", scopes, bindings, [{"dispatch_id": "d1", "status": "unknown"}], 20
        )


def test_custom_chart_task_reserves_repair_attempt():
    """图表任务额度必须覆盖声明用途 +1 次修复，即使派单少写了额度（WLY-20261108）。"""
    module = importlib.import_module("research_copilot.sandbox.execution_policy")
    services = importlib.import_module("research_copilot.subagents.services")
    contracts = importlib.import_module("research_copilot.contracts")
    # bind 规范化：计划批准额度 2，图表 brief 声明 2 项用途 → scope 应补齐到 3。
    brief = contracts.TaskBrief(
        task_id="T3",
        role="analysist",
        objective="生成图表",
        as_of="2025-04-30",
        calculation_mode="custom",
        execution_purposes=["register_metrics", "render_chart"],
        max_executions=2,
    )
    limit, purposes = services.execution_authorization({}, brief)
    assert (limit, purposes) == (
        3,
        ["register_metrics", "render_chart", "render_chart_repair"],
    )
    scopes = {
        "d1": {
            "analysis_execution_limit": limit,
            "execution_purposes": purposes,
            "task_brief": brief.model_dump(),
        },
    }
    bindings = {"d1": {"inputs": {"plan": {"content_hash": "p"}}}}
    records = [
        {"dispatch_id": "d1", "status": "registered"},
        {"dispatch_id": "d1", "status": "invalid_result", "output": "bad chart"},
    ]
    # 用途 2 项 + 修复 1 次 = 3：首跑失败后的修复执行应放行。
    module.check_execution_policy("d1", scopes, bindings, records, 20)
    records.append(
        {"dispatch_id": "d1", "status": "invalid_result", "output": "still bad"}
    )
    with pytest.raises(RuntimeError, match="count"):
        module.check_execution_policy("d1", scopes, bindings, records, 20)


def test_declarative_task_keeps_default_two_attempts():
    """标准计算任务不受图表修复额度兜底影响。"""
    services = importlib.import_module("research_copilot.subagents.services")
    contracts = importlib.import_module("research_copilot.contracts")
    brief = contracts.TaskBrief(
        task_id="T4",
        role="analysist",
        objective="标准同比",
        as_of="2025-04-30",
        required_metrics=[{"key": "revenue_growth", "semantic_type": "growth", "unit": "%"}],
    )
    limit, purposes = services.execution_authorization(
        {"analysis_execution_limit": 2}, brief
    )
    assert (limit, purposes) == (2, [])


def test_preflight_rejects_unconfirmed_rules_and_accepts_explicit_test_mode():
    module = importlib.import_module("research_copilot.runtime.preflight")
    rules = {"is_example": True, "content_hash": "abc", "version": 1}
    with pytest.raises(ValueError, match="example"):
        module.validate_rules(rules, mode="production")
    assert module.validate_rules(rules, mode="test")["mode"] == "test"
    rules["is_example"] = False
    with pytest.raises(ValueError, match="approval"):
        module.validate_rules(rules, mode="production")
    approved = {
        "rules_hash": "abc",
        "rules_version": 1,
        "actor_id": "researcher",
        "confirmed_at": "2026-10-30T10:00:00+08:00",
    }
    assert (
        module.validate_rules(rules, mode="production", approval=approved)["approval"]
        == approved
    )


def test_new_task_id_cannot_hide_unknown_or_inflate_authorized_limit():
    module = importlib.import_module("research_copilot.sandbox.execution_policy")
    scopes = {
        "old": {"task_brief": {"task_id": "a"}},
        "new": {"task_brief": {"task_id": "b", "max_executions": 10}},
    }
    bindings = {k: {"inputs": {"plan": {"content_hash": "p"}}} for k in scopes}
    with pytest.raises(RuntimeError, match="unknown"):
        module.check_execution_policy(
            "new", scopes, bindings, [{"dispatch_id": "old", "status": "unknown"}], 20
        )
    with pytest.raises(RuntimeError, match="count"):
        module.check_execution_policy(
            "new",
            scopes,
            bindings,
            [{"dispatch_id": "old", "status": "failed", "output": "bad"}] * 2,
            20,
        )
