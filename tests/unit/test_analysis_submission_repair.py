"""Real dispatch admission and publishing with immutable execution provenance."""

import hashlib
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.runtime.control import RunControl, active_dispatch
from research_copilot.subagents.analysist import AnalysisService
from research_copilot.subagents.contracts import AnalysisCandidate
from research_copilot.subagents.middleware import RoleBusinessMiddleware
from research_copilot.subagents.services import digest
from research_copilot.subagents.source_context import analysis_context, writing_context
from research_copilot.workspace import bundled_resources
from tests.unit.test_autonomous_dispatch import setup
from tests.unit.test_orchestration import context


async def prepared(tmp_path, repair_id="repair"):
    repo, business, services = await setup(tmp_path)
    source = {
        "value": "20.69",
        "published_at": "2025-01-01",
        "title": "Fixture disclosure",
    }
    repo.state.role_data["evidence"] = {
        "E-000001": {
            "raw": source,
            "content_hash": digest(source),
            "kind": "tool_original",
            "source": {"server": "test"},
            "collected_at": "2025-01-01",
        }
    }
    original = Dispatch("original", "analysist", "/workspace/tasks/original/")
    task = TaskBrief(
        task_id="original",
        role="analysist",
        objective="margin",
        as_of="2025-04-30",
        calculation_mode="custom",
    )
    await services.prepare_dispatch(context(), original, task.model_dump_json())
    root = services.report_root(context())
    path = root / "artifacts/original/receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    metric = {
        "schema_version": 2,
        "metric_id": "M-aaaaaaaaaaaaaaaa",
        "local_key": "margin",
        "value": "20.69",
        "unit": "%",
        "semantic_type": "gross_margin",
        "formula_id": "custom",
        "display_precision": 2,
        "is_estimate": False,
        "execution_id": "exec-original",
        "formula_version": "sandbox-code-v1",
        "evidence_ids": ["E-000001"],
        "inputs": [],
    }
    repo.state.role_data["metrics"] = {"original": {metric["metric_id"]: metric}}
    repo.state.role_data["executions"] = {
        "exec-original": {
            "execution_id": "exec-original",
            "dispatch_id": "original",
            "status": "registered",
            "code_hash": "c" * 64,
            "input_hash": "i" * 64,
            "code_paths": [],
            "evidence_hashes": {"E-000001": digest(source)},
            "files": {
                "artifacts/original/receipt.json": hashlib.sha256(b"{}").hexdigest()
            },
            "response": {"metric_ids": [metric["metric_id"]], "chart_ids": []},
        }
    }
    repo.state.role_data["dispatch_failures"] = {
        "original": {"code": "invalid_subagent_result", "repairable": True}
    }
    repair = Dispatch(repair_id, "analysist", f"/workspace/tasks/{repair_id}/")
    await services.prepare_dispatch(
        context(),
        repair,
        task.model_copy(
            update={"task_id": "repair", "repair_of": "original"}
        ).model_dump_json(),
    )
    return repo, business, services, repair, metric, path


def candidate(mid):
    return AnalysisCandidate(
        summary="毛利率20.69%",
        conclusions=["毛利率20.69%"],
        evidence_ids=["E-000001"],
        metric_ids=[mid],
        claims=[
            {
                "claim_id": "gm",
                "text": "毛利率20.69%",
                "metric_ids": [mid],
                "evidence_ids": ["E-000001"],
            }
        ],
    )


async def test_repair_inherits_receipts_and_writer_sees_original_values(tmp_path):
    repo, _, services, repair, metric, _ = await prepared(tmp_path)
    material = repo.state.role_data["scopes"][repair.dispatch_id]
    assert {
        m["metric_id"]
        for m in analysis_context(repo.state, material, repair.dispatch_id)[
            "registered_metrics"
        ]
    } == {metric["metric_id"]}
    token = active_dispatch.set(repair)
    try:
        await AnalysisService(services).publish_structured(
            context(), candidate(metric["metric_id"]), "repair-submit"
        )
    finally:
        active_dispatch.reset(token)
    written = writing_context(
        repo.state,
        material,
        "writer",
        resources_root=bundled_resources(),
        report_type="company_report",
    )
    assert written["analysis"]["metrics"][0]["value"] == "20.69"
    assert len(repo.state.role_data["executions"]) == 1


@pytest.mark.parametrize("invalid_first", [False, True])
async def test_native_truncation_repair_reaches_writer_without_reexecution(
    tmp_path, invalid_first
):
    from research_copilot.orchestration import RoleBinding
    from research_copilot.orchestration.middleware import dispatch_identity
    from research_copilot.subagents.analysist import build_analysis_tools
    from research_copilot.subagents.contracts import AnalysisResult, WritingResult
    from tests.unit.test_orchestration import ScriptModel, assemble, call, config

    repair_id = dispatch_identity("report-1", "repair-call")
    repo, business, services, _, metric, _ = await prepared(
        tmp_path / "workspace", repair_id
    )
    invalid = candidate(metric["metric_id"]).model_copy(update={"summary": "毛利率99%"})
    analysis_model = ScriptModel(
        script=[
            AIMessage(content="", response_metadata={"finish_reason": "length"}),
            *(
                [
                    call(
                        "submit_analysis",
                        {"candidate": invalid.model_dump()},
                        "bad-submit",
                    )
                ]
                if invalid_first
                else []
            ),
            call(
                "submit_analysis",
                {"candidate": candidate(metric["metric_id"]).model_dump()},
                "submit",
            ),
        ]
    )
    writer_model = ScriptModel(
        script=[
            call(
                "WritingResult",
                {
                    "task_id": "write",
                    "status": "blocked",
                    "summary": "Writer received analysis",
                    "report_type": "company_report",
                    "gaps": [
                        {
                            "description": "fixture ends at writer",
                            "impact": "offline handoff only",
                            "suggested_role": "supervisor",
                        }
                    ],
                },
            )
        ]
    )
    parent = ScriptModel(
        script=[
            call(
                "task",
                {
                    "subagent_type": "analysist",
                    "description": repo.state.role_data["scopes"][repair_id][
                        "description"
                    ],
                },
                "repair-call",
            ),
            call(
                "task",
                {
                    "subagent_type": "writer",
                    "description": TaskBrief(
                        task_id="write",
                        role="writer",
                        objective="write",
                        as_of="2025-04-30",
                    ).model_dump_json(),
                },
                "write-call",
            ),
            AIMessage(content="finished"),
        ]
    )
    roles = {
        "searcher": RoleBinding(
            ScriptModel(script=[AIMessage(content="unused")]), [], implemented=False
        ),
        "analysist": RoleBinding(
            analysis_model,
            build_analysis_tools(services),
            response_schema=AnalysisResult,
        ),
        "writer": RoleBinding(writer_model, [], response_schema=WritingResult),
    }
    agent = assemble(
        tmp_path, parent, business=business, roles=roles, autonomous_workflow=True
    )
    await agent.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert analysis_model.position == (3 if invalid_first else 2)
    if invalid_first:
        feedback = [
            m for m in analysis_model.messages_seen if isinstance(m, ToolMessage)
        ]
        assert any(
            "99" in str(m.content) and "invalid_candidate" in str(m.content)
            for m in feedback
        )
        calls = {
            c["id"]
            for m in analysis_model.messages_seen
            if isinstance(m, AIMessage)
            for c in m.tool_calls
        }
        assert all(m.tool_call_id in calls for m in feedback)
    assert writer_model.position == 1
    assert repo.state.results[repair_id]["status"] == "completed"
    assert len(repo.state.role_data["executions"]) == 1


async def test_compact_handoff_includes_authorized_evidence_for_metric_claims(tmp_path):
    repo, _, _, repair, metric, _ = await prepared(tmp_path)
    material = repo.state.role_data["scopes"][repair.dispatch_id]
    compact = analysis_context(repo.state, material, repair.dispatch_id)
    assert compact["evidence_ids"] == ["E-000001"]
    assert compact["registered_metrics"][0]["evidence_ids"] == ["E-000001"]
    assert (
        compact["registered_metrics"][0]["display_precision"]
        == metric["display_precision"]
    )


def test_large_tool_validation_error_preserves_the_actionable_field_error():
    from research_copilot.subagents.middleware import normalize_business_message

    message = ToolMessage(
        tool_call_id="bad",
        name="submit_analysis",
        status="error",
        content="Error invoking tool with kwargs "
        + "candidate payload " * 500
        + " with error:\n candidate.claims.0.limitations: Input should be a valid list",
    )
    result = normalize_business_message(message)
    assert "candidate.claims.0.limitations" in result.content
    assert len(result.content) < 2000


async def test_writer_and_reviewer_accept_authorized_original_execution(tmp_path):
    from research_copilot.subagents.contracts import ReportSection
    from research_copilot.subagents.writer import WritingService
    from research_copilot.subagents.review_snapshot import SnapshotService

    repo, _, services, repair, metric, _ = await prepared(tmp_path)
    token = active_dispatch.set(repair)
    try:
        saved = await AnalysisService(services).publish_structured(
            context(), candidate(metric["metric_id"]), "save"
        )
    finally:
        active_dispatch.reset(token)
    writer = Dispatch("write", "writer", "/workspace/tasks/write/")
    await services.prepare_dispatch(
        context(),
        writer,
        TaskBrief(
            task_id="write", role="writer", objective="write", as_of="2025-04-30"
        ).model_dump_json(),
    )
    template = (
        bundled_resources() / "skills/writer/reports/references/company_report.md"
    ).read_text("utf-8")
    sections = [
        ReportSection(
            heading=h,
            text="毛利率20.69%。"
            + "来源支持的观察需要结合口径限制解读。" * 25
            + "[^E-000001]",
            claims=[
                {
                    "quote": "毛利率20.69%。",
                    "metric_ids": [metric["metric_id"]],
                    "evidence_ids": ["E-000001"],
                    "conclusion_id": "gm",
                }
            ],
        )
        for h in re.findall(r"^## (.+)$", template, re.M)
    ]
    token = active_dispatch.set(writer)
    try:
        await WritingService(services).publish_structured(
            context(),
            title="报告",
            sections=sections,
            report_type="company_report",
            analysis_ids=[saved["analysis_id"]],
            submission_identity="write",
        )
    finally:
        active_dispatch.reset(token)
    review = Dispatch("review", "reviewer", "/workspace/tasks/review/")
    await services.prepare_dispatch(
        context(),
        review,
        TaskBrief(
            task_id="review", role="reviewer", objective="review", as_of="2025-04-30"
        ).model_dump_json(),
    )
    token = active_dispatch.set(review)
    try:
        service = SnapshotService(services)
        snapshot = await service.prepare_snapshot(context())
        content = await service.load_snapshot(context(), snapshot)
        assert (
            content["validation"]["execution_receipts"]["exec-original"]["dispatch_id"]
            == "original"
        )
        checked = await service.precheck(context(), snapshot)
        assert not [i for i in checked["issues"] if i["category"] == "metric"]
    finally:
        active_dispatch.reset(token)
    assert "analysis" not in repo.state.approved


@pytest.mark.parametrize("corrupt", ["files", "group", "scope", "metric", "execution"])
async def test_repair_cannot_borrow_or_tamper_with_receipts(tmp_path, corrupt):
    repo, _, services, repair, metric, path = await prepared(tmp_path)
    if corrupt == "files":
        path.write_text("changed", encoding="utf-8")
    elif corrupt == "group":
        repo.state.dispatches["original"]["work_group"] = "foreign"
    elif corrupt == "scope":
        repo.state.dispatches["original"]["inputs"]["scope"]["version"] = "other"
    elif corrupt == "metric":
        repo.state.role_data["metrics"]["original"][metric["metric_id"]]["value"] = "99"
    else:
        repo.state.role_data["executions"]["exec-original"]["code_hash"] = "changed"
    token = active_dispatch.set(repair)
    try:
        with pytest.raises((ValueError, PermissionError)):
            await AnalysisService(services).publish_structured(
                context(), candidate(metric["metric_id"]), "bad"
            )
    finally:
        active_dispatch.reset(token)
    assert "analysis" not in repo.state.artifacts


async def test_submission_only_repair_cannot_spend_execution(tmp_path):
    from research_copilot.sandbox.receipts import ExecutionService

    repo, _, services, repair, _, _ = await prepared(tmp_path)
    execution = ExecutionService(services, control=RunControl(), budget=None)
    token = active_dispatch.set(repair)
    try:
        with pytest.raises(ValueError, match="submission.only"):
            await execution.execute(context(), "new", "python code.py")
    finally:
        active_dispatch.reset(token)
    assert len(repo.state.role_data["executions"]) == 1


async def test_saved_analysis_survives_truncated_final_without_another_model_call(
    tmp_path,
):
    repo, _, services, repair, metric, _ = await prepared(tmp_path)
    token = active_dispatch.set(repair)
    try:
        saved = await AnalysisService(services).publish_structured(
            context(), candidate(metric["metric_id"]), "saved"
        )
        middleware = RoleBusinessMiddleware("analysist", services, RunControl())
        result = await middleware.abefore_model(
            {
                "messages": [
                    AIMessage(content="", response_metadata={"finish_reason": "length"})
                ]
            },
            SimpleNamespace(context=context()),
        )
        assert result["jump_to"] == "end"
        assert result["structured_response"].analysis_id == saved["analysis_id"]
        await services.map_result(
            context(), repair, result["structured_response"].model_dump()
        )
    finally:
        active_dispatch.reset(token)
    assert len(repo.state.role_data["executions"]) == 1
