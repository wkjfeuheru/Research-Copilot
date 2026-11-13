"""Execution attempts belong to a frozen work item, not a transient dispatch ID."""

from research_copilot.runtime.budget import BudgetExceeded
from research_copilot.sandbox.runner import ExecutionUnknown
import shlex
import ast
import re
from pathlib import PurePosixPath


def validate_model_command(command, write_prefix):
    """Model execution is a saved computation, never a filesystem discovery tool."""
    if not isinstance(command, str) or any(
        c in command for c in (";", "|", "&", "\n", "\r", "`", "$", "<", ">")
    ):
        raise ValueError(
            "execute accepts one saved Python task script, without shell operators"
        )
    parts = shlex.split(command)
    if len(parts) < 2 or parts[0] not in {"python", "python3"}:
        raise ValueError(
            "Use write_file to save a calculation script, then python <write_prefix>/run.py; use ls/read_file for inspection"
        )
    path = PurePosixPath(parts[1])
    if (
        not parts[1].startswith(write_prefix)
        or ".." in path.parts
        or path.suffix != ".py"
    ):
        raise ValueError(
            "Execute a .py file inside the authorized write_prefix; inline code and inspection commands are not computation submissions"
        )
    return parts[1]


def validate_python_source(source):
    """Static checks only: never evaluate model code on the host."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(
            f"Python syntax error at line {exc.lineno}: {exc.msg}"
        ) from exc
    conversion = re.compile(
        r"%(?:%|(?:\([^)]+\))?[#0 +\-]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?[diouxXeEfFgGcrsa])"
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            text = node.left.value
            position = 0
            while (position := text.find("%", position)) >= 0:
                match = conversion.match(text, position)
                if not match:
                    raise ValueError(
                        f"Invalid percent-format string at line {node.lineno}; escape literal % as %% or use an f-string"
                    )
                position = match.end()


def work_key(dispatch_id, scopes, bindings):
    if group := bindings.get(dispatch_id, {}).get("work_group"):
        return (group, None)
    inputs = bindings.get(dispatch_id, {}).get("inputs", {})
    return (inputs.get("plan"), inputs.get("rework"))


def execution_window_start(records, key, extensions, *, now):
    """An explicit host retry approval starts a new window, never erases attempts."""
    from datetime import datetime

    start = min((r["started_at"] for r in records), default=now)
    ids = {r["execution_id"] for r in records}
    for grant in extensions:
        prior = grant.get("prior_execution_ids", [])
        additional = grant.get("additional_attempts")
        if not (
            (grant.get("plan"), grant.get("rework")) == key
            and grant.get("actor_id")
            and grant.get("reason")
            and type(additional) is int
            and 0 < additional <= 20
            and prior
            and len(prior) == len(set(prior))
            and set(prior) <= ids
        ):
            continue
        try:
            date = datetime.fromisoformat(grant["authorized_at"])
            if date.tzinfo is None:
                continue
            timestamp = date.timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if start < timestamp <= now:
            start = timestamp
    return start


def check_execution_policy(
    dispatch_id, scopes, bindings, records, server_limit, *, extensions=()
):
    key = work_key(dispatch_id, scopes, bindings)
    matching = [
        r for r in records if work_key(r["dispatch_id"], scopes, bindings) == key
    ]
    if any(r["status"] in {"started", "unknown", "output_ready"} for r in matching):
        raise ExecutionUnknown(
            "Work item contains an unknown or unregistered execution; recover its existing receipt"
        )
    brief = scopes.get(dispatch_id, {}).get("task_brief") or {}
    brief_limit = brief.get("max_executions", 2)
    scope_limit = scopes.get(dispatch_id, {}).get("analysis_execution_limit", 2)
    if brief.get("calculation_mode") == "custom":
        # 图表/自定义分析包含脚本修复重试：额度必须覆盖全部声明用途 +1 次修复，
        # 不依赖模型在派单时自觉预留（提示词约束不稳定，WLY-20261108 验收教训）。
        brief_limit = max(brief_limit, len(brief.get("execution_purposes", []) or []) + 1)
        scope_limit = max(scope_limit, brief_limit)
    limit = min(
        brief_limit,
        scope_limit,
        server_limit,
    )
    explicit_remaining_attempt = False
    for grant in extensions:
        prior = grant.get("prior_execution_ids", [])
        additional = grant.get("additional_attempts")
        if (
            (grant.get("plan"), grant.get("rework")) == key
            and grant.get("actor_id")
            and grant.get("reason")
            and type(additional) is int
            and 0 < additional <= 20
            and prior
            and len(prior) == len(set(prior))
            and set(prior) <= {r.get("execution_id") for r in matching}
        ):
            limit = min(server_limit, max(limit, len(prior) + additional))
            explicit_remaining_attempt |= len(matching) < len(prior) + additional
    if len(matching) >= limit:
        raise BudgetExceeded(
            "Sandbox work item execution count exhausted",
            details={
                "code": "sandbox_execution_limit",
                "used": len(matching),
                "limit": limit,
            },
        )
    if matching and matching[-1]["status"] in {"cancelled", "timeout"}:
        raise BudgetExceeded(
            "Cancelled or timed out execution requires explicit diagnosis"
        )
    if (
        matching
        and matching[-1]["status"] in {"failed", "invalid_result"}
        and not (
            matching[-1].get("output") or matching[-1].get("response", {}).get("error")
        )
    ):
        raise ExecutionUnknown("Missing failure evidence; automatic repair forbidden")
    if (
        any(r["status"] == "registered" for r in matching)
        and not explicit_remaining_attempt
    ):
        purposes = scopes.get(dispatch_id, {}).get("execution_purposes", [])
        if len(purposes) <= len(matching) or not str(purposes[len(matching)]).strip():
            raise BudgetExceeded(
                "Additional successful execution needs a frozen task purpose"
            )
    return matching


def check_analysis_continuation(
    dispatch_id, scopes, bindings, records, server_limit, *, extensions=()
):
    """Stop futile model turns, but let registered calculations be submitted."""
    key = work_key(dispatch_id, scopes, bindings)
    matching = [
        r for r in records if work_key(r["dispatch_id"], scopes, bindings) == key
    ]
    if any(r["status"] in {"started", "unknown", "output_ready"} for r in matching):
        raise ExecutionUnknown(
            "Recover the existing execution before another analysis model call"
        )
    if any(r["status"] == "registered" for r in matching):
        return
    check_execution_policy(
        dispatch_id, scopes, bindings, records, server_limit, extensions=extensions
    )
