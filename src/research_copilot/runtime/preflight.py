"""Offline preflight; no network probe, credential output or implicit approval."""

import json
from pathlib import Path
import tomllib
from research_copilot.runtime.budget import TokenBudget


def validate_rules(rules, *, mode="production", approval=None):
    if mode not in {"production", "test"}:
        raise ValueError("Unknown execution mode")
    if mode == "production":
        if rules.get("test_only"):
            raise ValueError("Production cannot use test-only compliance rules")
        if rules.get("is_example", True):
            raise ValueError("Production cannot use example compliance rules")
        if (
            not approval
            or approval.get("rules_hash") != rules["content_hash"]
            or approval.get("rules_version") != rules["version"]
            or not approval.get("actor_id")
            or not approval.get("confirmed_at")
        ):
            raise ValueError(
                "Researcher approval must match the rules version and hash"
            )
        from research_copilot.subagents.source_policy import parse_research_as_of

        parse_research_as_of(approval["confirmed_at"])
    return {"mode": mode, "rules_hash": rules["content_hash"], "approval": approval}


def load_execution_policy(path=Path("config/execution_policy.toml")):
    policy = tomllib.loads(Path(path).read_text("utf-8"))
    if policy.get("version") != 1:
        raise ValueError("Unsupported execution policy")
    if any(
        type(policy.get(k)) is not int or policy[k] <= 0
        for k in ("token_limit", "call_limit")
    ):
        raise ValueError("Positive global limits required")
    if policy.get("soft_warning_fraction", 0.8) != 0.8:
        raise ValueError("Supported soft warning threshold is 80 percent")
    stages = policy["stages"]
    if set(stages) != {"supervisor", "searcher", "analysist", "writer", "reviewer"}:
        raise ValueError("Execution policy must declare all stages")
    for limits in stages.values():
        if any(
            type(limits.get(k)) is not int or limits[k] <= 0
            for k in ("calls", "tokens")
        ):
            raise ValueError("Positive stage limits required")
    TokenBudget(
        limit=policy["token_limit"],
        budget_id="preflight",
        call_limit=policy["call_limit"],
        stage_limits=stages,
        prices=policy.get("prices"),
    )
    return policy


def new_budget(
    budget_id, *, policy_path=Path("config/execution_policy.toml"), token_limit=None
):
    policy = load_execution_policy(policy_path)
    return TokenBudget(
        limit=min(token_limit or policy["token_limit"], policy["token_limit"]),
        budget_id=budget_id,
        call_limit=policy["call_limit"],
        stage_limits=policy["stages"],
        prices=policy.get("prices"),
    )


def preflight(
    *,
    rules_path=Path("config/compliance_rules.yaml"),
    mode="production",
    approval_path=None,
    policy_path=Path("config/execution_policy.toml"),
):
    from research_copilot.runtime.report_rules import load_report_rules
    from research_copilot.workspace import bundled_resources

    bundled_resources()
    policy = load_execution_policy(policy_path)
    rules = load_report_rules(rules_path).snapshot()
    approval = (
        json.loads(Path(approval_path).read_text("utf-8")) if approval_path else None
    )
    return rules, {
        **validate_rules(rules, mode=mode, approval=approval),
        "execution_policy": policy,
    }
