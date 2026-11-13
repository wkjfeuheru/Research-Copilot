"""Writer integration validates actual Markdown and freezes complete report versions."""

import json
import re
import pytest
from research_copilot.runtime.control import active_dispatch
from research_copilot.workspace import bundled_resources
from tests.integration.test_searcher import dispatch
from tests.integration.test_analysist import financial_evidence
from tests.unit.test_orchestration import context, config, call


async def confirmed_analysis(env, *, quantitative=False):
    from research_copilot.subagents.analysist import AnalysisService

    await financial_evidence(env)
    services, _, store = env
    scope = await dispatch(env, "analysist", "analysis-1")
    token = active_dispatch.set(scope)
    try:
        metric_ids = []
        if quantitative:
            from tests.integration.test_analysist import _seed_metric

            metric = await _seed_metric(
                store, scope, "M-" + "b" * 16, ["E-000001", "E-000002"]
            )
            metric_ids = [metric["metric_id"]]
        path = services.report_root(context()) / "tasks/analysis-1/a.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "summary": "Supported analysis",
                    "claims": [
                        {
                            "claim_id": "growth",
                            "text": "Revenue evidence is available",
                            "metric_ids": metric_ids,
                            "evidence_ids": ["E-000001", "E-000002"],
                            "required_in_report": False,
                        }
                    ]
                    if quantitative
                    else [],
                    "conclusions": ["Revenue evidence is available"],
                    "evidence_ids": ["E-000001", "E-000002"],
                    "metric_ids": metric_ids,
                    "chart_ids": [],
                    "assumptions": [],
                    "limitations": ["Historical availability unknown"],
                }
            ),
            encoding="utf-8",
        )
        receipt = await AnalysisService(services).save_candidate(
            context(), "save-a", "/workspace/tasks/analysis-1/a.json"
        )
        await store.mutate_control(
            context(),
            lambda state: state.approved.update(analysis=state.artifacts["analysis"]),
        )
        return receipt["analysis_id"]
    finally:
        active_dispatch.reset(token)


def markdown(services, report_type="company_report"):
    from research_copilot.workspace import bundled_resources

    template = (
        bundled_resources() / f"skills/writer/reports/references/{report_type}.md"
    ).read_text("utf-8")
    sections = re.findall(r"^## (.+)$", template, re.M)
    return "# Research\n\n" + "\n\n".join(
        "## "
        + s
        + "\n\n"
        + ("Source-supported observations and limitations. " * 9)
        + "[^E-000001]"
        for s in sections
    )


@pytest.mark.asyncio
async def test_writer_revalidates_after_edit_and_freezes_generated_sources(role_env):
    from research_copilot.subagents.writer import WritingService

    analysis_id = await confirmed_analysis(role_env)
    services, _, store = role_env
    scope = await dispatch(role_env, "writer", "writing-1")
    token = active_dispatch.set(scope)
    try:
        service = WritingService(services)
        path = services.report_root(context()) / "tasks/writing-1/draft.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown(services), encoding="utf-8")
        args = dict(
            candidate_path="/workspace/tasks/writing-1/draft.md",
            report_type="company_report",
            analysis_ids=[analysis_id],
        )
        assert (await service.validate(context(), **args))["ok"]
        path.write_text(
            markdown(services) + "\nFabricated [^E-999999]", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="evidence"):
            await service.submit(context(), "bad-submit", **args)
        path.write_text(markdown(services), encoding="utf-8")
        receipt = await service.submit(context(), "submit", **args)
        assert receipt["draft_version"] == 1
        assert receipt == await service.submit(context(), "submit", **args)
        repeated = await service.submit(context(), "new-tool-call", **args)
        assert repeated["artifact"] == receipt["artifact"]
        assert repeated["draft_version"] == 1
        state = await store.read_control(context())
        full = state.role_data["objects"][receipt["draft_id"]]["payload"]["document"]
        assert "[^E-000001]:" in full
        assert "Historical availability unknown" in full
        assert "report" not in state.approved
        assert state.reviewed_report is None
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_native_writer_reads_skill_template_edits_and_submits(role_env, tmp_path):
    from langchain_core.messages import HumanMessage
    from research_copilot.subagents.writer import build_writing_tools
    from research_copilot.subagents.contracts import WritingResult
    from tests.integration.support import RespondingModel, native_role, tool_outputs

    analysis_id = await confirmed_analysis(role_env)

    def respond(messages, step):
        envelope = json.loads(
            next(m.content for m in messages if isinstance(m, HumanMessage))
        )
        path = envelope["write_prefix"] + "draft.md"
        if step == 0:
            return call(
                "read_file", {"file_path": "/skills/writer/reports/SKILL.md"}, "entry"
            )
        if step == 1:
            return call(
                "read_file",
                {"file_path": "/skills/writer/reports/references/company_report.md"},
                "template",
            )
        if step == 2:
            return call(
                "write_file",
                {"file_path": path, "content": "# Incomplete"},
                "draft",
            )
        if step == 3:
            return call(
                "validate_draft",
                {
                    "candidate_path": path,
                    "report_type": "company_report",
                    "analysis_ids": [analysis_id],
                },
                "validate",
            )
        if step == 4:
            return call(
                "submit_draft",
                {
                    "candidate_path": path,
                    "report_type": "company_report",
                    "analysis_ids": [analysis_id],
                },
                "invalid-submit",
            )
        if step == 5:
            error = tool_outputs(messages)["submit_draft"]
            assert error["error"]["code"] == "invalid_candidate"
            return call(
                "edit_file",
                {
                    "file_path": path,
                    "old_string": "# Incomplete",
                    "new_string": markdown(role_env[0]),
                },
                "fix",
            )
        if step == 6:
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
        return call(
            "WritingResult",
            {
                "task_id": "write",
                "status": "completed",
                "summary": "Draft submitted",
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
    state = await role_env[2].read_control(context())
    assert state.artifacts["report"].version == "1"
    assert "report" not in state.approved


@pytest.mark.asyncio
async def test_wrong_report_type_and_template_structure_are_not_submitted(role_env):
    from research_copilot.subagents.writer import WritingService

    analysis_id = await confirmed_analysis(role_env)
    services, _, _ = role_env
    scope = await dispatch(role_env, "writer", "writing-bad")
    token = active_dispatch.set(scope)
    try:
        path = services.report_root(context()) / "tasks/writing-bad/draft.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Empty report", encoding="utf-8")
        service = WritingService(services)
        result = await service.validate(
            context(),
            candidate_path="/workspace/tasks/writing-bad/draft.md",
            report_type="company_report",
            analysis_ids=[analysis_id],
        )
        assert not result["ok"]
        assert any("section" in e for e in result["errors"])
        with pytest.raises(ValueError, match="type"):
            await service.submit(
                context(),
                "wrong",
                candidate_path="/workspace/tasks/writing-bad/draft.md",
                report_type="industry_report",
                analysis_ids=[analysis_id],
            )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_writer_expands_a_repeated_claim_quote_to_every_occurrence(role_env):
    from research_copilot.subagents.contracts import ReportSection, SectionClaim
    from research_copilot.subagents.writer import WritingService

    analysis_id = await confirmed_analysis(role_env)
    services, _, store = role_env
    scope = await dispatch(role_env, "writer", "writing-repeat")
    template = (
        bundled_resources() / "skills/writer/reports/references/company_report.md"
    ).read_text("utf-8")
    headings = re.findall(r"^## (.+)$", template, re.M)
    body_text = "Source-supported observations and limitations. " * 9 + "[^E-000001]"
    repeated = "重复的风险说明适用于本节。"
    sections = [
        ReportSection(
            heading=heading,
            text=body_text
            + ("\n\n" + repeated + "\n\n" + repeated if index == 0 else ""),
            claims=[SectionClaim(quote=repeated, evidence_ids=["E-000001"])]
            if index == 0
            else [],
        )
        for index, heading in enumerate(headings)
    ]
    token = active_dispatch.set(scope)
    try:
        receipt = await WritingService(services).publish_structured(
            context(),
            title="Repeated claim",
            sections=sections,
            report_type="company_report",
            analysis_ids=[analysis_id],
            submission_identity="structured-repeat",
        )
        state = await store.read_control(context())
        manifest = state.role_data["objects"][receipt["draft_id"]]["payload"]["claims"]
        assert len(manifest["claims"]) == 2
        assert {claim["quote"] for claim in manifest["claims"]} == {repeated}
        document = state.role_data["objects"][receipt["draft_id"]]["payload"][
            "document"
        ]
        assert document.count(repeated) == 2
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_writer_locates_a_paraphrased_claim_by_its_metric_value(role_env):
    from research_copilot.subagents.contracts import ReportSection, SectionClaim
    from research_copilot.subagents.writer import WritingService

    analysis_id = await confirmed_analysis(role_env, quantitative=True)
    metric_id = "M-" + "b" * 16
    services, _, store = role_env
    scope = await dispatch(role_env, "writer", "writing-locate")
    template = (
        bundled_resources() / "skills/writer/reports/references/company_report.md"
    ).read_text("utf-8")
    headings = re.findall(r"^## (.+)$", template, re.M)
    body_text = "Source-supported observations and limitations. " * 9 + "[^E-000001]"
    sections = [
        ReportSection(
            heading=heading,
            text=body_text + (" Growth was 0.20倍[^E-000001]" if index == 0 else ""),
            claims=[
                SectionClaim(
                    quote="growth improved materially",
                    metric_ids=[metric_id],
                    evidence_ids=["E-000001", "E-000002"],
                    conclusion_id="growth",
                )
            ]
            if index == 0
            else [],
        )
        for index, heading in enumerate(headings)
    ]
    token = active_dispatch.set(scope)
    try:
        receipt = await WritingService(services).publish_structured(
            context(),
            title="Located claim",
            sections=sections,
            report_type="company_report",
            analysis_ids=[analysis_id],
            submission_identity="structured-locate",
        )
        state = await store.read_control(context())
        manifest = state.role_data["objects"][receipt["draft_id"]]["payload"]["claims"]
        assert len(manifest["claims"]) == 1
        assert "0.20" in manifest["claims"][0]["quote"]
        assert manifest["claims"][0]["conclusion_id"] == "growth"
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_native_writer_submits_structured_sections_without_files(
    role_env, tmp_path
):
    """The structured result itself is the submission: no write_file, no reads."""
    from research_copilot.subagents.writer import build_writing_tools
    from research_copilot.subagents.contracts import WritingResult
    from tests.integration.support import RespondingModel, native_role

    analysis_id = await confirmed_analysis(role_env)
    template = (
        bundled_resources() / "skills/writer/reports/references/company_report.md"
    ).read_text("utf-8")
    headings = re.findall(r"^## (.+)$", template, re.M)
    body_text = "Source-supported observations and limitations. " * 9 + "[^E-000001]"

    def respond(messages, step):
        if step == 0:
            return call(
                "WritingResult",
                {
                    "task_id": "write",
                    "status": "completed",
                    "summary": "Structured sections submission",
                    "report_type": "company_report",
                    "analysis_ids": [analysis_id],
                    "title": "Structured research",
                    "sections": [
                        {"heading": heading, "text": body_text, "claims": []}
                        for heading in headings
                    ],
                    "gaps": [],
                },
                "result",
            )
        raise AssertionError("writer should finish in one structured turn")

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
    _, _, store = role_env
    state = await store.read_control(context())
    assert result["messages"][-1].content == "done"
    assert state.artifacts["report"].version == "1"
    writer_dispatch = next(
        ident
        for ident, binding in state.dispatches.items()
        if binding["role"] == "writer"
    )
    task_results = state.role_data["task_results"][writer_dispatch]
    assert task_results["status"] == "completed"
    document = state.role_data["objects"][state.artifacts["report"].artifact_id][
        "payload"
    ]["document"]
    assert document.startswith("# Structured research")
    assert "## " + headings[0] in document
