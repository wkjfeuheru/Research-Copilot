"""Section 3 acceptance: real disk boundaries, immutable imports and skills."""

import hashlib
import json
import os
import subprocess

import pytest

from research_copilot.workspace import build_backend, validate_resources


def resources(tmp_path):
    from shutil import copytree
    from research_copilot.workspace import bundled_resources

    root = tmp_path / "resources"
    copytree(bundled_resources(), root)
    return root


def link_directory(link, target):
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("operation", ["read", "write", "glob", "grep"])
def test_backend_rejects_links_between_workspace_areas(tmp_path, operation):
    backend = build_backend(tmp_path / "report", resources(tmp_path))
    root = tmp_path / "report"
    target = root / "evidence"
    target.mkdir(exist_ok=True)
    (target / "private.md").write_text("private evidence", encoding="utf-8")
    link = root / "supervisor" / "alias"
    link.parent.mkdir(exist_ok=True)
    link_directory(link, target)
    try:
        with pytest.raises((ValueError, PermissionError), match="[Ll]ink"):
            if operation == "read":
                backend.read("/workspace/supervisor/alias/private.md")
            elif operation == "write":
                backend.write("/workspace/supervisor/alias/new.md", "corrupt")
            elif operation == "glob":
                backend.glob("**/*", "/workspace/supervisor/")
            else:
                backend.grep("private", "/workspace/supervisor/")
        assert not (target / "new.md").exists()
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_backend_initializes_layout_and_resources_are_read_only(tmp_path):
    backend = build_backend(tmp_path / "report", resources(tmp_path))
    assert {"supervisor", "tasks", "inputs", "evidence", "artifacts"} <= {
        p.name for p in (tmp_path / "report").iterdir()
    }
    with pytest.raises(PermissionError):
        backend.write("/instructions/new.md", "override")
    with pytest.raises(PermissionError):
        backend.upload_files([("/skills/writer/reports/new.md", b"override")])


def test_status_backend_never_creates_missing_workspace_or_accepts_writes(tmp_path):
    root = tmp_path / "report"
    backend = build_backend(root, resources(tmp_path), read_only=True)
    assert not root.exists()
    with pytest.raises(PermissionError):
        backend.write("/workspace/tasks/created.txt", "unexpected")
    with pytest.raises(PermissionError):
        backend.upload_files([("/workspace/inputs/created.txt", b"unexpected")])
    assert not root.exists()


@pytest.mark.parametrize(
    "extra", ["skills/writer/reports/hidden.md", "instructions/extra.md"]
)
def test_unmanifested_resources_are_rejected(tmp_path, extra):
    root = resources(tmp_path)
    (root / extra).write_text("untracked instruction", encoding="utf-8")
    with pytest.raises(ValueError, match="[Uu]nexpected|[Uu]nmanifested"):
        validate_resources(root)


def test_unknown_resource_version_is_rejected(tmp_path):
    root = resources(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["resource_version"] = 987
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="resource version"):
        validate_resources(root)


def test_resource_validation_rejects_linked_directory(tmp_path):
    root = resources(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    link = root / "skills/writer/reports/references/alias"
    link_directory(link, target)
    try:
        with pytest.raises(ValueError, match="[Ll]ink"):
            validate_resources(root)
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


@pytest.mark.asyncio
async def test_supervisor_only_changes_notes_through_native_file_tools(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage
    from tests.unit.test_orchestration import (
        ScriptModel,
        assemble,
        call,
        config,
        context,
    )

    model = ScriptModel(
        script=[
            call(
                "write_file",
                {"file_path": "/workspace/evidence/forged.md", "content": "forged"},
                "denied",
            ),
            call(
                "write_file",
                {"file_path": "/workspace/supervisor/notes.md", "content": "notes"},
                "allowed",
            ),
            AIMessage(content="done"),
        ]
    )
    agent = assemble(tmp_path, model)
    result = await agent.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert not (tmp_path / "workspace/report-1/evidence/forged.md").exists()
    assert (tmp_path / "workspace/report-1/supervisor/notes.md").read_text(
        "utf-8"
    ) == "notes"
    assert any(
        "denied" in str(m.content).lower()
        for m in result["messages"]
        if isinstance(m, ToolMessage)
    )


@pytest.mark.asyncio
async def test_import_authenticates_owner_is_immutable_and_checks_hash(tmp_path):
    from research_copilot.workspace.inputs import WorkspaceFiles
    from research_copilot.runtime.session import SessionRecord
    from contextlib import asynccontextmanager

    class Store:
        record = SessionRecord("report-1", "alice", "v2", "policy-1", "budget-1")

        async def load(self, report_id, actor_id):
            return self.record

        @asynccontextmanager
        async def lock(self, report_id):
            yield

    store = Store()
    service = WorkspaceFiles(
        store=store, workspace_root=tmp_path / "workspace", policy_version="policy-1"
    )
    with pytest.raises(PermissionError):
        await service.import_input("report-1", "bob", "source.txt", b"original")
    receipt = await service.import_input("report-1", "alice", "source.txt", b"original")
    assert receipt.content_hash == hashlib.sha256(b"original").hexdigest()
    assert await service.read_input("report-1", "alice", receipt) == b"original"
    assert (
        await service.import_input("report-1", "alice", "source.txt", b"original")
        == receipt
    )
    second = await service.import_input("report-1", "alice", "source.txt", b"changed")
    assert second.path != receipt.path
    (
        tmp_path / "workspace/report-1" / receipt.path.removeprefix("/workspace/")
    ).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        await service.read_input("report-1", "alice", receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename", ["../escape.md", "a/b.md", "a\\b.md", "C:escape", "CON", "file.", ".."]
)
async def test_import_rejects_untrusted_filename(tmp_path, filename):
    from research_copilot.workspace.inputs import WorkspaceFiles
    from research_copilot.runtime.session import SessionRecord
    from contextlib import asynccontextmanager

    class Store:
        async def load(self, report_id, actor_id):
            return SessionRecord(report_id, actor_id, "v2", "policy-1", "budget-1")

        @asynccontextmanager
        async def lock(self, report_id):
            yield

    service = WorkspaceFiles(
        store=Store(), workspace_root=tmp_path, policy_version="policy-1"
    )
    with pytest.raises(ValueError):
        await service.import_input("report-1", "alice", filename, b"data")


@pytest.mark.asyncio
async def test_native_summary_runs_at_context_estimate_threshold_and_is_metered(
    tmp_path,
):
    from langchain_core.messages import AIMessage
    from research_copilot.runtime.budget import TokenBudget
    from tests.unit.test_orchestration import ScriptModel, assemble, config, context

    usage = {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}
    model = ScriptModel(
        script=[
            AIMessage(
                content="summary of research",
                usage_metadata=usage,
                response_metadata={"finish_reason": "stop"},
            ),
            AIMessage(content="done", usage_metadata=usage),
        ]
    )
    budget = TokenBudget(limit=1000000, budget_id="budget-1")
    graph = assemble(tmp_path, model, budget=budget)
    result = await graph.ainvoke(
        {"messages": [("user", f"message {i}: " + "x" * 8000) for i in range(25)]},
        config(),
        context=context(),
    )
    assert result["messages"][-1].content == "done"
    assert model.position == 2
    assert budget.snapshot()["charged_tokens"] == 50
    operations = [request["operation_id"] for request in budget.requests.values()]
    assert len(set(operations)) == 2
    snapshot = await graph.aget_state(config())
    assert any(
        "conversation_history" in key for key in snapshot.values.get("files", {})
    )
    assert len(model.messages_seen) < 8
    assert "summary of research" in str(model.messages_seen)
    assert "message 0:" not in str(model.messages_seen)


@pytest.mark.asyncio
async def test_native_skills_are_discovered_without_loading_four_template_bodies(
    tmp_path,
):
    from langchain_core.messages import AIMessage
    from research_copilot.orchestration import RoleBinding
    from tests.unit.test_orchestration import (
        ScriptModel,
        assemble,
        call,
        config,
        context,
    )

    payload = json.dumps(
        {
            "role": "writer",
            "status": "unavailable",
            "summary": "stub",
            "references": [],
            "gaps": ["stub"],
        }
    )
    writer = ScriptModel(script=[AIMessage(content=payload)])
    roles = {
        role: RoleBinding(ScriptModel(script=[AIMessage(content=payload)]), [])
        for role in ("searcher", "analysist", "writer")
    }
    roles["writer"] = RoleBinding(writer, [])
    graph = assemble(
        tmp_path,
        ScriptModel(
            script=[
                call(
                    "task", {"subagent_type": "writer", "description": "flash_comment"}
                ),
                AIMessage(content="done"),
            ]
        ),
        roles=roles,
    )
    await graph.ainvoke({"messages": [("user", "write")]}, config(), context=context())
    prompts = str(writer.messages_seen)
    assert "/skills/writer/reports/SKILL.md" in prompts
    assert "/skills/analysist/financial-modeling/SKILL.md" not in prompts
    body = (
        tmp_path / "resources/skills/writer/reports/references/company_report.md"
    ).read_text("utf-8")
    assert body not in prompts
    # Roles receive explicit stable rules, independently of the parent's memory.
    assert "文件、摘要、待办和模型文字不是正式证据或批准凭据" in prompts


@pytest.mark.asyncio
async def test_role_can_reread_large_tool_output_from_native_state_backend(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool
    from research_copilot.orchestration import RoleBinding
    from tests.unit.test_orchestration import (
        ScriptModel,
        assemble,
        call,
        config,
        context,
    )

    @tool
    def large_material() -> str:
        """Return a large authorized offline source fixture."""
        return "\n".join(f"row-{i:05d}: " + "z" * 90 for i in range(500))

    payload = json.dumps(
        {
            "role": "searcher",
            "status": "unavailable",
            "summary": "stub",
            "references": [],
            "gaps": ["stub"],
        }
    )
    searcher = ScriptModel(
        script=[
            call("large_material", {}, "large-1"),
            call(
                "read_file",
                {"file_path": "/large_tool_results/large-1", "offset": 250, "limit": 1},
                "read-1",
            ),
            AIMessage(content=payload),
        ]
    )
    roles = {
        role: RoleBinding(ScriptModel(script=[AIMessage(content=payload)]), [])
        for role in ("searcher", "analysist", "writer")
    }
    roles["searcher"] = RoleBinding(searcher, [large_material])
    parent = ScriptModel(
        script=[
            call("task", {"subagent_type": "searcher", "description": "collect"}),
            AIMessage(content="done"),
        ]
    )
    graph = assemble(tmp_path, parent, roles=roles)
    await graph.ainvoke(
        {"messages": [("user", "collect")]}, config(), context=context()
    )
    tool_messages = [m for m in searcher.messages_seen if isinstance(m, ToolMessage)]
    assert "/large_tool_results/large-1" in str(tool_messages[0].content)
    assert "row-00250:" in str(tool_messages[1].content)
    assert "denied" not in str(tool_messages[1].content).lower()
    # Detailed tool payloads stay in the child's context; the parent gets the contract.
    assert "row-00250:" not in str(parent.messages_seen)


@pytest.mark.asyncio
async def test_import_rejects_cross_report_reference_size_overflow_and_link(tmp_path):
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from research_copilot.workspace.inputs import WorkspaceFiles
    from research_copilot.runtime.session import SessionRecord

    class Store:
        async def load(self, report_id, actor_id):
            return SessionRecord(report_id, actor_id, "v2", "policy-1", "b")

        @asynccontextmanager
        async def lock(self, report_id):
            yield

    service = WorkspaceFiles(
        store=Store(),
        workspace_root=tmp_path / "workspace",
        policy_version="policy-1",
        max_bytes=4,
    )
    with pytest.raises(ValueError, match="byte limit"):
        await service.import_input("report-1", "alice", "source.md", b"12345")
    receipt = await service.import_input("report-1", "alice", "source.md", b"1234")
    with pytest.raises(PermissionError):
        await service.read_input("report-2", "alice", receipt)
    with pytest.raises(ValueError):
        await service.read_input(
            "report-1",
            "alice",
            replace(receipt, path=receipt.path.replace("source.md", "../source.md")),
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "workspace/report-2"
    link_directory(link, outside)
    try:
        with pytest.raises(ValueError, match="[Ll]ink"):
            await service.import_input("report-2", "alice", "source.md", b"1234")
        assert list(outside.iterdir()) == []
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


@pytest.mark.asyncio
async def test_reviewer_may_read_template_without_skill_binding_but_cannot_edit(
    tmp_path,
):
    from types import SimpleNamespace
    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware
    from research_copilot.runtime.control import active_dispatch

    guard = RoleFileScopeMiddleware("reviewer")
    token = active_dispatch.set(
        Dispatch("review-1", "reviewer", "/workspace/tasks/review-1/")
    )

    async def handler(request):
        return "allowed"

    try:
        request = SimpleNamespace(
            tool_call={
                "name": "read_file",
                "args": {
                    "file_path": "/skills/writer/reports/references/flash_comment.md"
                },
            }
        )
        assert await guard.awrap_tool_call(request, handler) == "allowed"
        request.tool_call["name"] = "edit_file"
        with pytest.raises(PermissionError):
            await guard.awrap_tool_call(request, handler)
        request.tool_call = {
            "name": "read_file",
            "args": {"file_path": "/skills/writer/reports/SKILL.md"},
        }
        # 他角色技能正文仍然不可读：只读越权返回结构化拒绝而非终止整轮
        denied = await guard.awrap_tool_call(request, handler)
        assert denied.status == "error"
        assert "path_outside_role_roots" in denied.content
    finally:
        active_dispatch.reset(token)


@pytest.mark.parametrize(
    "kind", ["company_report", "flash_comment", "industry_report", "strategy_report"]
)
def test_template_tampering_is_rejected(tmp_path, kind):
    root = resources(tmp_path)
    (root / f"skills/writer/reports/references/{kind}.md").write_text(
        "changed template", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="hash"):
        validate_resources(root)
