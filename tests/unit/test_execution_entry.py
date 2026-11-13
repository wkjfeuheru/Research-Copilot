import pytest


@pytest.mark.parametrize(
    "command",
    [
        "wc -c /workspace/a",
        "ls /workspace; ls /tmp",
        "python -c 'print(1)'",
        "python /workspace/tasks/d/run.py; ls /tmp",
    ],
)
def test_inspection_and_inline_shell_are_rejected_before_launch(command):
    from research_copilot.sandbox.execution_policy import validate_model_command

    with pytest.raises(ValueError):
        validate_model_command(command, "/workspace/tasks/d/")


def test_saved_task_script_is_a_valid_model_execution():
    from research_copilot.sandbox.execution_policy import validate_model_command

    validate_model_command(
        "python /workspace/tasks/d/run.py /workspace/tasks/d/input.json",
        "/workspace/tasks/d/",
    )


def test_explicit_execution_extension_retains_previous_attempts_and_binding():
    from research_copilot.sandbox.execution_policy import check_execution_policy

    scopes = {"d": {"task_brief": {"max_executions": 2}}}
    bindings = {"d": {"inputs": {"plan": {"content_hash": "p"}}}}
    records = [
        {"execution_id": str(i), "dispatch_id": "d", "status": "completed"}
        for i in range(2)
    ]
    grant = {
        "actor_id": "user",
        "reason": "explicit recovery approval",
        "plan": {"content_hash": "p"},
        "rework": None,
        "prior_execution_ids": ["0", "1"],
        "additional_attempts": 2,
    }
    assert (
        len(
            check_execution_policy(
                "d", scopes, bindings, records, 20, extensions=[grant]
            )
        )
        == 2
    )
    stale = {**grant, "plan": {"content_hash": "other"}}
    with pytest.raises(RuntimeError, match="count"):
        check_execution_policy("d", scopes, bindings, records, 20, extensions=[stale])
    with pytest.raises(RuntimeError, match="count"):
        check_execution_policy(
            "d",
            scopes,
            bindings,
            records + [dict(records[0], execution_id=str(i)) for i in (2, 3)],
            20,
            extensions=[grant],
        )


@pytest.mark.asyncio
async def test_model_inspection_is_rejected_without_calling_execution_backend():
    from types import SimpleNamespace
    from research_copilot.subagents.middleware import RoleBusinessMiddleware
    from research_copilot.runtime.control import RunControl
    import json

    class Services:
        async def scope(self, context, role):
            return SimpleNamespace(write_prefix="/workspace/tasks/d/"), None

    middleware = RoleBusinessMiddleware("analysist", Services(), RunControl())
    request = SimpleNamespace(
        state={},
        runtime=SimpleNamespace(context=None),
        tool_call={
            "id": "diagnostic",
            "name": "execute",
            "args": {"command": "ls /workspace"},
        },
    )

    async def forbidden(request):
        raise AssertionError("Execution backend must not be called")

    result = await middleware.awrap_tool_call(request, forbidden)
    payload = json.loads(result.content)
    assert payload["error"]["attempt_consumed"] is False
    assert payload["error"]["container_started"] is False


@pytest.mark.asyncio
async def test_model_script_format_preflight_does_not_start_container(tmp_path):
    from types import SimpleNamespace
    from research_copilot.subagents.middleware import RoleBusinessMiddleware
    from research_copilot.runtime.control import RunControl
    import json

    script = tmp_path / "tasks/d/run.py"
    script.parent.mkdir(parents=True)
    script.write_text('description = "37.15%); %.2f" % value', encoding="utf-8")

    class Services:
        async def scope(self, context, role):
            return SimpleNamespace(write_prefix="/workspace/tasks/d/"), None

        def report_root(self, context):
            return tmp_path

    request = SimpleNamespace(
        state={},
        runtime=SimpleNamespace(context=None),
        tool_call={
            "id": "calculation",
            "name": "execute",
            "args": {"command": "python /workspace/tasks/d/run.py"},
        },
    )

    async def forbidden(request):
        raise AssertionError("must reject before backend")

    result = await RoleBusinessMiddleware(
        "analysist", Services(), RunControl()
    ).awrap_tool_call(request, forbidden)
    error = json.loads(result.content)["error"]
    assert error["attempt_consumed"] is False and "line 1" in error["message"]


def test_explicit_retry_grant_allows_remaining_attempt_after_registration():
    from research_copilot.sandbox.execution_policy import check_execution_policy

    scopes = {"d": {"task_brief": {"max_executions": 1}}}
    bindings = {"d": {"inputs": {"plan": "p"}}}
    records = [
        {
            "execution_id": "old",
            "dispatch_id": "d",
            "status": "failed",
            "output": "bad",
        },
        {"execution_id": "new", "dispatch_id": "d", "status": "registered"},
    ]
    grant = {
        "plan": "p",
        "rework": None,
        "actor_id": "user",
        "reason": "correct rejected analysis",
        "prior_execution_ids": ["old"],
        "additional_attempts": 2,
    }
    assert (
        len(
            check_execution_policy(
                "d", scopes, bindings, records, 20, extensions=[grant]
            )
        )
        == 2
    )
    with pytest.raises(RuntimeError, match="count"):
        check_execution_policy(
            "d",
            scopes,
            bindings,
            records + [dict(records[-1], execution_id="last")],
            20,
            extensions=[grant],
        )
