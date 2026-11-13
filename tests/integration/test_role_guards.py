"""Adversarial business boundary and stale review regressions."""

import json
import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from research_copilot.runtime.control import active_dispatch
from tests.integration.test_searcher import dispatch
from tests.integration.test_writer import confirmed_analysis, markdown
from tests.integration.test_reviewer import (
    submitted_draft,
    reviewer,
    reviewing_model,
)
from tests.integration.support import native_role
from tests.unit.test_orchestration import context, config, call


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["hash", "dispatch"])
async def test_receipt_mismatch_splits_forgery_from_stale_receipt(role_env, mismatch):
    from research_copilot.subagents.services import MissingSubmissionReceipt

    services, _, store = role_env
    scope = await dispatch(role_env)
    ref = {"artifact_id": "submitted", "version": "1", "content_hash": "a" * 64}

    def register(state):
        state.role_data.setdefault("objects", {})["submitted"] = {
            "ref": ref,
            "dispatch_id": scope.dispatch_id if mismatch == "hash" else "other",
        }

    await store.mutate_control(context(), register)
    claimed = {**ref, "content_hash": "b" * 64} if mismatch == "hash" else ref
    with pytest.raises(ValueError, match="submission receipt") as exc:
        await services.map_result(
            context(),
            scope,
            {
                "task_id": "collect",
                "status": "completed",
                "summary": "bad receipt",
                "artifact_refs": [claimed],
            },
        )
    if mismatch == "dispatch":
        # 引用其他派单的回执属越权伪造，保持不可修复
        assert not isinstance(exc.value, MissingSubmissionReceipt)
    else:
        # 同一派单内引用旧哈希属可修复结果，交由角色按服务端回执重写
        assert isinstance(exc.value, MissingSubmissionReceipt)
    assert not (await store.read_control(context())).role_data.get("task_results")


@pytest.mark.asyncio
async def test_partial_result_citing_foreign_artifact_is_repairable(role_env):
    """未完成结果引用其他派单产物属可修复：去掉引用重交，而不是终止整轮。"""
    from research_copilot.subagents.services import MissingSubmissionReceipt

    services, _, store = role_env
    scope = await dispatch(role_env)
    ref = {"artifact_id": "analysis-1", "version": "1", "content_hash": "a" * 64}

    def register(state):
        state.role_data.setdefault("objects", {})["analysis-1"] = {
            "ref": ref,
            "dispatch_id": "other-dispatch",
            "kind": "analysis",
        }

    await store.mutate_control(context(), register)
    with pytest.raises(MissingSubmissionReceipt, match="another dispatch"):
        await services.map_result(
            context(),
            scope,
            {
                "task_id": "collect",
                "status": "partial",
                "summary": "partial",
                "gaps": [
                    {
                        "description": "chart not rendered",
                        "impact": "no chart",
                        "suggested_role": "analysist",
                    }
                ],
                "artifact_refs": [ref],
            },
        )
    assert not (await store.read_control(context())).role_data.get("task_results")


@pytest.mark.asyncio
async def test_typed_task_identity_and_runtime_manifest_are_frozen(role_env):
    from research_copilot.subagents.contracts import TaskBrief

    services, _, _ = role_env
    services.runtime_manifest = {"sources": {"sina_read": "schema-a"}}
    brief = TaskBrief(
        task_id="brief-1",
        role="searcher",
        objective="Collect filings",
        as_of="2025-04-30",
    )
    from tests.integration.support import approve_task

    await approve_task(role_env, brief)
    scope = await dispatch(role_env, description=brief.model_dump_json())
    with pytest.raises(ValueError, match="task_id"):
        await services.map_result(
            context(),
            scope,
            {
                "task_id": "different",
                "status": "blocked",
                "summary": "No source",
                "gaps": [
                    {
                        "description": "No source",
                        "impact": "Cannot collect",
                        "suggested_role": "supervisor",
                    }
                ],
            },
        )
    services.runtime_manifest = {"sources": {"sina_read": "schema-b"}}
    with pytest.raises(ValueError, match="configuration"):
        await dispatch(role_env, ident="changed-config")


@pytest.mark.asyncio
async def test_reviewer_independently_rejects_unknown_citation_in_frozen_appendix(
    role_env,
):
    from research_copilot.subagents.services import canonical, digest

    await submitted_draft(role_env)
    services, _, store = role_env

    def corrupt(state):
        draft = state.artifacts["report"]
        obj = state.role_data["objects"][draft.artifact_id]
        obj["payload"]["document"] += (
            "\nUnknown generated appendix source [^E-999999]\n"
        )
        changed = draft.model_copy(update={"content_hash": digest(obj["payload"])})
        obj["ref"] = changed.model_dump()
        (
            services.report_root(context()) / obj["path"].removeprefix("/workspace/")
        ).write_text(canonical(obj["payload"]), encoding="utf-8")
        state.artifacts["report"] = changed

    await store.mutate_control(context(), corrupt)
    service, _, _ = await reviewer(role_env)
    scope = await dispatch(role_env, "reviewer", "independent-check")
    token = active_dispatch.set(scope)
    try:
        snapshot = await service.prepare_snapshot(context())
        result = await service.precheck(context(), snapshot)
        assert any(
            issue["category"] == "reference" and issue["hard"]
            for issue in result["issues"]
        )
        content = await service.load_snapshot(context(), snapshot)
        assert (
            len(
                [
                    target
                    for target in content["targets"].values()
                    if target["kind"] == "evidence"
                ]
            )
            == 2
        )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_completed_analysis_result_cannot_forge_business_ids(role_env):
    from research_copilot.subagents.contracts import AnalysisResult

    services, _, store = role_env
    await confirmed_analysis(role_env)
    state = await store.read_control(context())
    scope = next(
        key for key, value in state.dispatches.items() if value["role"] == "analysist"
    )
    from research_copilot.contracts import Dispatch

    with pytest.raises(ValueError, match="analysis"):
        await services.map_result(
            context(),
            Dispatch(scope, "analysist", f"/workspace/tasks/{scope}/"),
            AnalysisResult(
                task_id="analysis",
                status="completed",
                summary="forged",
                analysis_id="forged",
                artifact_refs=[state.artifacts["analysis"]],
            ).model_dump(),
        )


@pytest.mark.asyncio
async def test_writer_cannot_read_superseded_unconfirmed_analysis(role_env):
    services, _, store = role_env
    old = await confirmed_analysis(role_env)
    old_object = (await store.read_control(context())).role_data["objects"][old]

    def replace(state):
        from research_copilot.contracts import ArtifactRef

        ref = ArtifactRef(
            artifact_id="analysis-current", version="2", content_hash="c" * 64
        )
        state.artifacts["analysis"] = ref
        state.approved["analysis"] = ref
        state.role_data["objects"]["analysis-current"] = {
            **old_object,
            "ref": ref.model_dump(),
        }

    await store.mutate_control(context(), replace)
    scope = await dispatch(role_env, "writer", "writer-new")
    token = active_dispatch.set(scope)
    try:
        with pytest.raises(PermissionError):
            await services.get_object(context(), old, kind="analysis")
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_reviewer_returns_stale_if_draft_changes_during_human_wait(
    role_env, tmp_path
):
    await submitted_draft(role_env)
    service, graph, models = await reviewer(
        role_env, (reviewing_model(0.84), reviewing_model())
    )
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        budget=service.models["quality"].budget,
    )
    first = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    pending = first["__interrupt__"][0]
    positions = [m.position for m in models]
    from research_copilot.contracts import ArtifactRef

    await role_env[2].mutate_control(
        context(),
        lambda state: state.artifacts.update(
            report=ArtifactRef(
                artifact_id="new-draft", version="2", content_hash="e" * 64
            )
        ),
    )
    value = {
        "decision": "request_changes",
        "comment": "new version",
        "review_session_id": pending.value["review_session_id"],
        "snapshot_id": pending.value["snapshot"]["snapshot_id"],
        "issue_dispositions": [],
    }
    done = await agent.ainvoke(
        Command(resume={pending.id: value}), config(), context=context()
    )
    assert not done.get("__interrupt__")
    assert positions == [m.position for m in models]
    state = await role_env[2].read_control(context())
    assert next(iter(state.role_data["reviews"].values()))["outcome"] == "stale"
    assert state.reviewed_report is None


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0.849, 0.85, 0.851])
async def test_reviewer_threshold_and_incomplete_coverage_routes(
    role_env, tmp_path, score
):
    await submitted_draft(role_env)
    service, graph, _ = await reviewer(
        role_env, (reviewing_model(score), reviewing_model(0.99, incomplete=True))
    )
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        budget=service.models["quality"].budget,
    )
    result = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    assert result["__interrupt__"]
    assert not result["__interrupt__"][0].value["assessment"]["checks_complete"]


@pytest.mark.asyncio
async def test_reviewer_branch_failure_does_not_turn_into_human_content_review(
    role_env, tmp_path
):
    await submitted_draft(role_env)
    from tests.integration.support import RespondingModel

    def failed(messages, step):
        raise RuntimeError("provider failed")

    service, graph, _ = await reviewer(
        role_env, (RespondingModel(script=[], respond=failed), reviewing_model())
    )
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        budget=service.models["quality"].budget,
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        await agent.ainvoke(
            {"messages": [("user", "review")]}, config(), context=context()
        )
    state = await role_env[2].read_control(context())
    assert not state.role_data.get("reviews")
    assert state.reviewed_report is None


@pytest.mark.asyncio
async def test_host_injected_template_counts_as_read_and_content_still_validated(
    role_env, tmp_path
):
    """Writer template delivery is host-side; submission passes without any /skills read."""
    from research_copilot.subagents.writer import build_writing_tools
    from research_copilot.subagents.contracts import WritingResult
    from tests.integration.support import RespondingModel, tool_outputs
    from langchain_core.messages import HumanMessage

    analysis_id = await confirmed_analysis(role_env)

    def respond(messages, step):
        envelope = json.loads(
            next(m.content for m in messages if isinstance(m, HumanMessage))
        )
        path = envelope["write_prefix"] + "draft.md"
        if step == 0:
            return call(
                "write_file",
                {"file_path": path, "content": markdown(role_env[0])},
                "candidate",
            )
        if step == 1:
            return call(
                "submit_draft",
                {
                    "candidate_path": path,
                    "report_type": "company_report",
                    "analysis_ids": [analysis_id],
                },
                "submit",
            )
        receipt = tool_outputs(messages)["submit_draft"]
        assert "draft_id" in receipt, receipt
        return call(
            "WritingResult",
            {
                "task_id": "write",
                "status": "completed",
                "summary": "Draft submitted without skill reads",
                "draft_id": receipt["draft_id"],
                "draft_version": receipt["draft_version"],
                "report_type": "company_report",
                "analysis_ids": [analysis_id],
                "artifact_refs": [receipt["artifact"]],
                "evidence_ids": ["E-000001"],
                "gaps": [],
            },
            "result",
        )

    model = RespondingModel(script=[], respond=respond)
    agent = await native_role(
        role_env,
        tmp_path,
        "writer",
        model,
        build_writing_tools(role_env[0]),
        WritingResult,
    )
    result = await agent.ainvoke(
        {"messages": [("user", "write")]}, config(), context=context()
    )
    assert result["messages"][-1].content == "done"
    # Not a single read_file call: the injected template satisfied the gate.
    assert not [m for m in result["messages"] if getattr(m, "name", "") == "read_file"]
    _, _, store = role_env
    state = await store.read_control(context())
    writer_dispatch = next(
        ident
        for ident, binding in state.dispatches.items()
        if binding["role"] == "writer"
    )
    reads = state.role_data["scopes"][writer_dispatch]["skill_reads"]
    assert "/skills/writer/reports/SKILL.md" in reads
    assert "/skills/writer/reports/references/company_report.md" in reads
    assert state.artifacts["report"].version == "1"


@pytest.mark.asyncio
async def test_writer_cannot_read_unconfirmed_template_reference(role_env, tmp_path):
    from research_copilot.subagents.writer import build_writing_tools
    from research_copilot.subagents.contracts import WritingResult
    from tests.integration.support import RespondingModel, tool_outputs
    from langchain_core.messages import AIMessage

    await confirmed_analysis(role_env)
    observed = {}

    def respond(messages, step):
        if step == 0:
            return call(
                "read_file",
                {"file_path": "/skills/writer/reports/references/flash_comment.md"},
                "other-template",
            )
        observed.update(tool_outputs(messages))
        return AIMessage(content="stop")

    agent = await native_role(
        role_env,
        tmp_path,
        "writer",
        RespondingModel(script=[], respond=respond),
        build_writing_tools(role_env[0]),
        WritingResult,
    )
    result = await agent.ainvoke(
        {"messages": [("user", "write")]}, config(), context=context()
    )
    output = observed["read_file"]
    assert output["error"]["code"] == "skill_entry_required"
    assert "confirmed template" in output["error"]["message"]
    # The run ends gracefully: the role never produced a valid structured
    # result, and the supervisor receives a task-level error instead of a crash.
    assert result["messages"][-1].content == "done"
    task_message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert json.loads(task_message.content).get("error") in {
        "role_result_repair_exhausted",
        "invalid_subagent_result",
    }


@pytest.mark.asyncio
async def test_imported_attachment_is_registered_internally_and_hash_bound(role_env):
    from research_copilot.workspace.inputs import InputFile
    import hashlib

    services, _, store = role_env
    raw = b"original uploaded material"
    sha = hashlib.sha256(raw).hexdigest()
    path = services.report_root(context()) / f"inputs/{sha}/filing.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    ref = InputFile("report-1", f"/workspace/inputs/{sha}/filing.txt", sha, len(raw))
    receipt = await services.register_attachment(context(), ref)
    assert receipt == await services.register_attachment(context(), ref)
    state = await store.read_control(context())
    assert (
        state.role_data["evidence"][receipt["evidence_id"]]["kind"]
        == "attachment_original"
    )
    assert (
        state.role_data["evidence"][receipt["evidence_id"]]["raw"]["body"]
        == "original uploaded material"
    )
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        await services.register_attachment(context(), ref)


@pytest.mark.asyncio
async def test_reviewer_result_requires_committed_review_receipt(role_env):
    from research_copilot.contracts import ReviewResult

    await submitted_draft(role_env)
    services, business, store = role_env
    scope = await dispatch(role_env, "reviewer", "forged-review")
    draft = (await store.read_control(context())).artifacts["report"]
    with pytest.raises(PermissionError, match="receipt"):
        await business.accept_result(
            context(),
            scope,
            ReviewResult(
                status="passed",
                summary="fake model approval",
                reviewed=draft,
                issues=[],
                comments=[],
            ),
        )


@pytest.mark.asyncio
async def test_review_changes_create_version_bound_rework_confirmation(
    role_env, tmp_path
):
    await submitted_draft(role_env)
    service, graph, _ = await reviewer(
        role_env, (reviewing_model(0.8), reviewing_model())
    )
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        budget=service.models["quality"].budget,
    )
    first = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    pending = first["__interrupt__"][0]
    value = {
        "decision": "request_changes",
        "comment": "Clarify evidence limitations",
        "review_session_id": pending.value["review_session_id"],
        "snapshot_id": pending.value["snapshot"]["snapshot_id"],
        "issue_dispositions": [],
    }
    await agent.ainvoke(
        Command(resume={pending.id: value}), config(), context=context()
    )
    state = await role_env[2].read_control(context())
    assert state.artifacts.get("rework")
    assert "rework" not in state.approved
    with pytest.raises(PermissionError, match="rework"):
        await dispatch(role_env, "writer", "unconfirmed-rework")


@pytest.mark.asyncio
async def test_sandbox_result_path_denied_without_aborting_registered_analysis():
    from types import SimpleNamespace
    from research_copilot.contracts import Dispatch
    from research_copilot.orchestration.middleware import RoleFileScopeMiddleware

    scope = Dispatch("d", "analysist", "/workspace/tasks/d/")
    request = SimpleNamespace(
        tool_call={
            "name": "read_file",
            "id": "read-result",
            "args": {"file_path": "/outputs/result.json"},
        }
    )
    token = active_dispatch.set(scope)
    try:

        async def forbidden(_):
            pytest.fail("container file must never be opened")

        reply = await RoleFileScopeMiddleware("analysist").awrap_tool_call(
            request, forbidden
        )
        assert reply.status == "error" and "registered_metrics" in reply.content
    finally:
        active_dispatch.reset(token)
