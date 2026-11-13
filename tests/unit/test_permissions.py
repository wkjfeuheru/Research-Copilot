from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["**/*execution*contract*", "../other/*", "C:\\private\\*"])
async def test_invalid_read_navigation_is_denied_without_aborting_research(path):
    import json

    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch

    request = SimpleNamespace(
        tool_call={"id": "live-relative-glob", "name": "glob", "args": {"pattern": path}}
    )

    async def forbidden(request):
        pytest.fail("Invalid navigation must not reach filesystem access")

    token = active_dispatch.set(Dispatch("own", "analysist", "/workspace/tasks/own/"))
    try:
        result = await RoleFileScopeMiddleware("analysist").awrap_tool_call(request, forbidden)
        assert result.status == "error"
        payload = json.loads(result.content)
        assert payload["error"] == "invalid_virtual_path"
        assert "/skills/analysist" in payload["allowed_roots"]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_role_can_list_own_task_root_without_trailing_slash():
    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch

    request = SimpleNamespace(
        tool_call={"name": "ls", "args": {"path": "/workspace/tasks/own"}}
    )

    async def handler(request):
        return "own directory"

    token = active_dispatch.set(Dispatch("own", "searcher", "/workspace/tasks/own/"))
    try:
        assert (
            await RoleFileScopeMiddleware("searcher").awrap_tool_call(request, handler)
            == "own directory"
        )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ls", "glob"])
@pytest.mark.parametrize("path", ["/workspace", "/workspace/tasks"])
async def test_workspace_root_discovery_returns_scoped_guidance_without_filesystem_access(
    name, path
):
    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch

    request = SimpleNamespace(
        tool_call={
            "id": "root-discovery",
            "name": name,
            "args": {"path": path, "pattern": "**/*"},
        }
    )

    async def handler(request):
        pytest.fail("Global directory must never be enumerated")

    token = active_dispatch.set(Dispatch("own", "searcher", "/workspace/tasks/own/"))
    try:
        result = await RoleFileScopeMiddleware("searcher").awrap_tool_call(
            request, handler
        )
        assert result.status == "error"
        assert "/workspace/evidence/own/" in result.content
        assert "/workspace/artifacts/own/" in result.content
        assert "/workspace/tasks/own/" in result.content
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_sandbox_wrapped_backend_retains_history_in_native_graph_state(tmp_path):
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    from langchain_core.messages import AIMessage
    from research_copilot.workspace import build_backend, bundled_resources
    from research_copilot.sandbox.backend import AnalysisSandboxBackend
    from research_copilot.orchestration import filesystem_middleware
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from tests.unit.test_orchestration import ScriptModel, call

    backend = AnalysisSandboxBackend(
        build_backend(tmp_path / "report", bundled_resources()), None
    )

    @tool
    async def offload_history():
        """Exercise the same StateBackend write used by native summarization."""
        result = await backend.awrite(
            "/conversation_history/session.md", "retained history"
        )
        assert not result.error
        return "saved"

    graph = create_agent(
        model=ScriptModel(
            script=[call("offload_history", {}), AIMessage(content="done")]
        ),
        tools=[offload_history],
        middleware=[
            RoleFileScopeMiddleware("analysist", allow_execute=True),
            filesystem_middleware(backend, execute=True),
        ],
    )
    result = await graph.ainvoke({"messages": [("user", "offload")]})
    assert (
        result["files"]["/conversation_history/session.md"]["content"]
        == "retained history"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,path",
    [
        ("write_file", "/workspace/evidence/source.md"),
        ("edit_file", "/skills/writer/reports/SKILL.md"),
        ("read_file", "/workspace/tasks/other/private.md"),
        ("write_file", "/workspace/tasks/own/../other/private.md"),
        ("write_file", "C:\\temp\\escape.md"),
        ("grep", "/"),
        ("read_file", "/skills/analysist/financial-modeling/SKILL.md"),
    ],
)
async def test_role_cannot_escape_its_server_allocated_task_directory(name, path):
    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch

    middleware = RoleFileScopeMiddleware("writer")
    request = SimpleNamespace(
        tool_call={"name": name, "args": {"file_path": path, "path": path}}
    )

    async def handler(request):
        pytest.fail("Unauthorized operation reached the filesystem")

    token = active_dispatch.set(Dispatch("own", "writer", "/workspace/tasks/own/"))
    try:
        if name in {"write_file", "edit_file"}:
            with pytest.raises(PermissionError):
                await middleware.awrap_tool_call(request, handler)
        else:
            # 只读越权返回结构化拒绝：模型可改正路径后继续，不终止整轮研究
            result = await middleware.awrap_tool_call(request, handler)
            assert result.status == "error"
            assert "path_outside_role_roots" in result.content
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_writer_reading_analysis_task_directory_gets_recoverable_denial():
    """第十一轮崩溃：writer 读取分析任务目录被判越权后整轮终止，应改为可恢复拒绝。"""
    import json

    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch

    request = SimpleNamespace(
        tool_call={
            "id": "read-chart-script",
            "name": "read_file",
            "args": {"file_path": "/workspace/tasks/bf79ef02/code/chart_overview.py"},
        }
    )

    async def handler(request):
        pytest.fail("另一个任务的目录不得进入文件系统")

    token = active_dispatch.set(Dispatch("own", "writer", "/workspace/tasks/own/"))
    try:
        result = await RoleFileScopeMiddleware("writer").awrap_tool_call(
            request, handler
        )
        payload = json.loads(result.content)
        assert payload["error"] == "path_outside_role_roots"
        assert "/workspace/artifacts/own" in payload["hint"]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern,expect_handler",
    [
        # glob 的目标路径在 pattern 参数里（WLY-20261108 第九轮崩溃缺陷）
        ("/skills/**/*.md", False),
        ("/workspace/evidence/**", True),
    ],
)
async def test_glob_pattern_is_validated_as_path(pattern, expect_handler):
    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch

    request = SimpleNamespace(
        tool_call={"id": "glob-call", "name": "glob", "args": {"pattern": pattern}}
    )
    reached = {}

    async def handler(request):
        reached["yes"] = True
        return "listed"

    token = active_dispatch.set(Dispatch("own", "analysist", "/workspace/tasks/own/"))
    try:
        result = await RoleFileScopeMiddleware("analysist").awrap_tool_call(
            request, handler
        )
        if expect_handler:
            assert reached.get("yes") and result == "listed"
        else:
            assert result.status == "error"
            assert "path_outside_role_roots" in result.content
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_role_has_no_task_or_execute_tools_in_real_assembly(tmp_path):
    from tests.unit.test_orchestration import (
        ScriptModel,
        assemble,
        call,
        config,
        context,
    )
    from research_copilot.orchestration import RoleBinding
    from langchain_core.messages import AIMessage
    import json

    supervisor = ScriptModel(
        script=[
            call("task", {"subagent_type": "analysist", "description": "analyze"}),
            AIMessage(content="done"),
        ]
    )
    roles = {
        name: RoleBinding(
            model=ScriptModel(
                script=[
                    AIMessage(
                        content=json.dumps(
                            {
                                "role": name,
                                "status": "unavailable",
                                "summary": "unavailable",
                                "references": [],
                                "gaps": ["not implemented"],
                            }
                        )
                    )
                ]
            ),
            tools=[],
        )
        for name in ("searcher", "analysist", "writer")
    }
    graph = assemble(tmp_path, supervisor, roles=roles)
    result = await graph.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert result["messages"][-1].content == "done"
    role_tools = {tool.name for tool in roles["analysist"].model.tools_seen}
    assert {
        "task",
        "execute",
        "propose_plan",
        "confirm_plan",
        "signoff_report",
    }.isdisjoint(role_tools)
    assert {"read_file", "write_file"} <= role_tools
    dispatch_message = roles["analysist"].model.messages_seen[-1]
    payload = json.loads(dispatch_message.content)
    assert payload["description"] == "analyze"
    assert payload["write_prefix"].startswith("/workspace/tasks/")
    assert payload["dispatch_id"] in payload["write_prefix"]


def test_backend_rejects_windows_junction_outside_report_root(tmp_path):
    import os
    import subprocess
    from deepagents.backends import FilesystemBackend

    root, outside = tmp_path / "report", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    link = root / "link"
    if os.name == "nt":
        # Test-controlled literal paths. Remove only the junction after the check.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        with pytest.raises(ValueError, match="outside root"):
            backend._resolve_path("/link/secret.md")
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
