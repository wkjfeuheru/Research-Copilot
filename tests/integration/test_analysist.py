"""Analysis submission accepts sandbox metric receipts and rejects unknown ones."""

import json
import pytest
from research_copilot.runtime.control import active_dispatch
from tests.integration.test_searcher import dispatch
from tests.unit.test_orchestration import context


async def financial_evidence(env):
    services, _, _ = env
    scope = await dispatch(env)
    token = active_dispatch.set(scope)
    try:
        for period, value in [("2024", "100"), ("2025", "120")]:

            async def fetch(args, p=period, v=value):
                return {
                    "context": {
                        "entity": "AAA",
                        "unit": "CNY",
                        "scale": "1",
                        "period": p,
                        "basis": "consolidated",
                    },
                    "revenue": v,
                }

            await services.acquire(
                context(), period, "financekb_fundamentals", "filing", {"period": period}, fetch
            )
    finally:
        active_dispatch.reset(token)


async def _seed_metric(store, scope, metric_id, evidence_ids):
    state = await store.read_control(context())
    evidence = state.role_data["evidence"]
    receipt = {
        "schema_version": 2,
        "local_key": "growth",
        "semantic_type": "growth",
        "formula_id": "custom",
        "numerator_basis": "revenue change",
        "denominator_basis": "prior revenue",
        "is_estimate": False,
        "display_precision": 2,
        "unit": "ratio",
        "metric_id": metric_id,
        "value": "0.2",
        "formula_version": "m3-formula-2",
        "evidence_ids": list(evidence_ids),
        "inputs": [
            {
                "evidence_id": ident,
                "content_hash": evidence[ident]["content_hash"],
            }
            for ident in evidence_ids
        ],
    }

    def write(state):
        state.role_data.setdefault("metrics", {}).setdefault(scope.dispatch_id, {})[
            metric_id
        ] = receipt

    await store.mutate_control(context(), write)
    return receipt


@pytest.mark.asyncio
async def test_analysis_submits_seeded_metric_without_approving(role_env):
    from research_copilot.subagents.analysist import AnalysisService

    await financial_evidence(role_env)
    services, _, store = role_env
    scope = await dispatch(role_env, "analysist", "analysis-1")
    token = active_dispatch.set(scope)
    analysis = AnalysisService(services)
    try:
        metric = await _seed_metric(
            store, scope, "M-" + "a" * 16, ["E-000001", "E-000002"]
        )
        assert not (await store.read_control(context())).artifacts.get("analysis")
        candidate = {
            "claims": [
                {
                    "claim_id": "growth",
                    "text": "Growth is supported by the filings",
                    "metric_ids": [metric["metric_id"]],
                    "evidence_ids": ["E-000001", "E-000002"],
                }
            ],
            "summary": "Revenue increased",
            "conclusions": ["Growth is supported by the filings"],
            "evidence_ids": ["E-000001", "E-000002"],
            "metric_ids": [metric["metric_id"]],
            "chart_ids": [],
            "assumptions": [],
            "limitations": [],
        }
        path = services.report_root(context()) / "tasks/analysis-1/candidate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidate), encoding="utf-8")
        receipt = await analysis.save_candidate(
            context(), "save-1", "/workspace/tasks/analysis-1/candidate.json"
        )
        assert receipt == await analysis.save_candidate(
            context(), "save-1", "/workspace/tasks/analysis-1/candidate.json"
        )
        state = await store.read_control(context())
        assert state.artifacts["analysis"].model_dump() == receipt["artifact"]
        assert "analysis" not in state.approved
        assert state.role_data["objects"][receipt["analysis_id"]]["payload"][
            "metric_ids"
        ] == [metric["metric_id"]]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_analysis_rejects_forged_metric_on_save(role_env):
    from research_copilot.subagents.analysist import AnalysisService

    await financial_evidence(role_env)
    services, _, store = role_env
    token = active_dispatch.set(await dispatch(role_env, "analysist", "analysis-bad"))
    try:
        analysis = AnalysisService(services)
        assert not (await store.read_control(context())).role_data.get("metrics")
        path = services.report_root(context()) / "tasks/analysis-bad/candidate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "summary": "forged",
                    "conclusions": ["fake"],
                    "evidence_ids": ["E-000001"],
                    "metric_ids": ["M-" + "f" * 16],
                    "chart_ids": [],
                    "assumptions": [],
                    "limitations": [],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="metric"):
            await analysis.save_candidate(
                context(), "bad", "/workspace/tasks/analysis-bad/candidate.json"
            )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
@pytest.mark.docker
async def test_native_analysis_loop_can_compute_render_and_submit(role_env, tmp_path):
    from langchain_core.messages import HumanMessage, ToolMessage
    from research_copilot.subagents.analysist import build_analysis_tools
    from research_copilot.subagents.contracts import AnalysisResult
    from tests.integration.support import RespondingModel, native_role, tool_outputs
    from tests.unit.test_orchestration import call, config

    await financial_evidence(role_env)
    state = await role_env[2].read_control(context())
    refs = []
    for i, (period, amount) in enumerate([("2024", "100"), ("2025", "120")], 1):
        eid = f"E-{i:06d}"
        refs.append(
            {
                "key": f"revenue{i}",
                "evidence_id": eid,
                "content_hash": state.role_data["evidence"][eid]["content_hash"],
                "pointer": "/revenue",
                "raw_value": amount,
                "unit": "CNY",
                "currency": "CNY",
                "period": period,
                "entity": "AAA",
                "basis": "consolidated",
            }
        )
    generated_code = """import json,os
from pathlib import Path
from growth import calculate
from charts import render
from io_contract import write_result
refs = REFS_PLACEHOLDER
items=[{'value':r['raw_value'],'context':{k:r.get(k,'1') for k in ('entity','unit','currency','period','basis','scale')}} for r in refs]
metric=calculate('yoy',items[1],items[0])
from decimal import Decimal, ROUND_HALF_UP
pct=(Decimal(metric['value'])*100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
out=Path(os.environ['RC_OUTPUT_DIR'])
render(kind='bar',title='Revenue growth / 营收增长',unit='%',data={'labels':['AAA'],'series':[{'name':'Growth','values':[str(pct)]}]},output_dir=str(out/'growth'),engine='echarts',metric_keys=['growth'],alt='Growth is 20 percent')
write_result(out/'result.json',metrics=[{'key':'growth','value':str(pct),'unit':'%','input_keys':['revenue2','revenue1'],'semantic_type':'growth','formula_id':'growth_percent','numerator_basis':'revenue 2025','denominator_basis':'revenue 2024','display_precision':2,'is_estimate':False}],input_refs=refs,charts=['growth/chart.json'])
""".replace("REFS_PLACEHOLDER", repr(refs))

    def execution_output(messages):
        message = next(
            m
            for m in reversed(messages)
            if isinstance(m, ToolMessage) and m.name == "execute"
        )
        return json.JSONDecoder().raw_decode(message.content)[0]

    def respond(messages, step):
        envelope = json.loads(
            next(m.content for m in messages if isinstance(m, HumanMessage))
        )
        outputs = tool_outputs(messages)
        if step == 0:
            # Progressive loading: read a script directly, with no SKILL.md page gate.
            return call(
                "read_file",
                {
                    "file_path": "/skills/analysist/financial-modeling/scripts/growth.py",
                    "limit": 20,
                },
                "reference",
            )
        if step == 1:
            return call(
                "write_file",
                {
                    "file_path": envelope["write_prefix"] + "run.py",
                    "content": 'raise ValueError("fixable code error")',
                },
                "bad-code",
            )
        if step == 2:
            return call(
                "execute",
                {"command": f"python {envelope['write_prefix']}run.py"},
                "failed-execution",
            )
        if step == 3:
            assert execution_output(messages)["status"] == "failed"
            return call(
                "edit_file",
                {
                    "file_path": envelope["write_prefix"] + "run.py",
                    "old_string": 'raise ValueError("fixable code error")',
                    "new_string": generated_code,
                },
                "fix-code",
            )
        if step == 4:
            return call(
                "execute",
                {"command": f"python {envelope['write_prefix']}run.py"},
                "successful-execution",
            )
        if step == 5:
            execution = execution_output(messages)
            assert execution["status"] == "registered", execution.get(
                "error", execution
            )
            return call(
                "write_file",
                {
                    "file_path": envelope["write_prefix"] + "analysis.json",
                    "content": json.dumps(
                        {
                            "summary": "Revenue growth",
                            "conclusions": ["20 percent increase"],
                            "evidence_ids": ["E-000001", "E-000002"],
                            "metric_ids": execution["metric_ids"],
                            "chart_ids": execution["chart_ids"],
                            "assumptions": [],
                            "limitations": [],
                            "claims": [
                                {
                                    "claim_id": "growth",
                                    "text": "20 percent increase",
                                    "metric_ids": execution["metric_ids"],
                                    "evidence_ids": ["E-000001", "E-000002"],
                                    "required_in_report": False,
                                }
                            ],
                        }
                    ),
                },
                "candidate",
            )
        if step == 6:
            return call(
                "save_analysis",
                {"candidate_path": envelope["write_prefix"] + "analysis.json"},
                "save",
            )
        receipt = outputs["save_analysis"]
        execution = execution_output(messages)
        return call(
            "AnalysisResult",
            {
                "task_id": "analysis",
                "status": "completed",
                "summary": "Analysis saved",
                "analysis_id": receipt["analysis_id"],
                "artifact_refs": [receipt["artifact"]],
                "evidence_ids": ["E-000001", "E-000002"],
                "metric_ids": execution["metric_ids"],
                "chart_ids": execution["chart_ids"],
                "gaps": [],
            },
            "result",
        )

    model = RespondingModel(script=[], respond=respond)
    agent = await native_role(
        role_env,
        tmp_path,
        "analysist",
        model,
        build_analysis_tools(role_env[0]),
        AnalysisResult,
    )
    output = await agent.ainvoke(
        {"messages": [("user", "analyze")]}, config(), context=context()
    )
    assert output["messages"][-1].content == "done"
    names = {t.name if hasattr(t, "name") else t["name"] for t in model.tools_seen}
    assert "execute" in names and not names & {
        "calc_growth",
        "calc_ratios",
        "calc_valuation",
        "calc_period_change",
        "render_chart",
        "build_comparison_table",
    }
    state = await role_env[2].read_control(context())
    assert state.artifacts["analysis"] and "analysis" not in state.approved
    chart = next(iter(next(iter(state.role_data["charts"].values())).values()))
    assert chart["receipt"]["engine"] == "echarts"
    from PIL import Image

    static = next(path for path in chart["files"] if path.endswith(".png"))
    with Image.open(role_env[0].report_root(context()) / static) as png:
        assert png.crop((0, 0, 1200, 70)).convert("L").getextrema()[0] < 100, (
            "SSR PNG lost all title glyphs"
        )
    assert len(list((tmp_path / "workspace").rglob("*.png"))) >= 1
    assert not any(s.get("skill_reads") for s in state.role_data["scopes"].values())
    # Continue through the real writer/snapshot boundary using fixture-only prior confirmation.
    await role_env[2].mutate_control(
        context(), lambda s: s.approved.update(analysis=s.artifacts["analysis"])
    )
    from tests.integration.test_writer import markdown
    from tests.integration.test_searcher import dispatch
    from research_copilot.subagents.writer import WritingService

    services = role_env[0]
    scope = await dispatch(role_env, "writer", "sandbox-writing")
    token = active_dispatch.set(scope)
    try:
        candidate = services.report_root(context()) / "tasks/sandbox-writing/draft.md"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(
            markdown(services)
            + "\n\n![Growth](chart:"
            + chart["receipt"]["chart_id"]
            + ")",
            encoding="utf8",
        )
        saved = await WritingService(services).submit(
            context(),
            "sandbox-draft",
            candidate_path=scope.write_prefix + "draft.md",
            report_type="company_report",
            analysis_ids=[state.artifacts["analysis"].artifact_id],
        )
        assert saved["draft_id"]
    finally:
        active_dispatch.reset(token)
    from tests.integration.test_reviewer import reviewer
    from langchain_core.messages import AIMessage

    def branch_fixture(vision=False):
        def respond(messages, step):
            payload = json.loads(
                next(m.content for m in messages if isinstance(m, HumanMessage))
            )
            targets = payload["targets"]
            if step == 0:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": t["tool"],
                            "args": {"target_id": t["target_id"]},
                            "id": f"read-{i}",
                        }
                        for i, t in enumerate(targets)
                    ],
                )
            return call(
                "BranchReview",
                {
                    "pass_confidence": 0.85,
                    "checks_complete": True,
                    "covered_targets": [t["target_id"] for t in targets],
                    "issues": [],
                },
                "branch-result",
            )

        model = RespondingModel(script=[], respond=respond)
        model.profile = {**model.profile, "image_inputs": vision}
        return model

    review, _, _ = await reviewer(
        role_env, models=(branch_fixture(True), branch_fixture())
    )
    scope = await dispatch(role_env, "reviewer", "sandbox-review")
    token = active_dispatch.set(scope)
    try:
        snapshot = await review.prepare_snapshot(context())
        content = await review.load_snapshot(context(), snapshot)
        kinds = {t["kind"] for t in content["targets"].values()}
        assert {"execution", "code", "chart_image"} <= kinds
        checked = await review.precheck(context(), snapshot)
        assert not [i for i in checked["issues"] if i["hard"]]
        result = await review.review_branch(context(), snapshot, checked, "quality")
        assert result["checks_complete"]
        # Explicitly incapable models must leave visual checks incomplete even at a high score.
        text_review, _, _ = await reviewer(
            role_env, models=(branch_fixture(), branch_fixture())
        )
        result = await text_review.review_branch(
            context(), snapshot, checked, "quality"
        )
        assert not result["checks_complete"]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_analysis_structured_submission_publishes_without_file(role_env):
    from research_copilot.subagents.analysist import AnalysisService
    from research_copilot.subagents.contracts import AnalysisCandidate, AnalysisClaim

    await financial_evidence(role_env)
    services, _, store = role_env
    scope = await dispatch(role_env, "analysist", "analysis-struct")
    token = active_dispatch.set(scope)
    try:
        analysis = AnalysisService(services)
        metric = await _seed_metric(
            store, scope, "M-" + "c" * 16, ["E-000001", "E-000002"]
        )
        candidate = AnalysisCandidate(
            summary="Revenue increased",
            conclusions=["Growth is supported by the filings"],
            evidence_ids=["E-000001", "E-000002"],
            metric_ids=[metric["metric_id"]],
            claims=[
                AnalysisClaim(
                    claim_id="growth",
                    text="Growth is supported by the filings",
                    metric_ids=[metric["metric_id"]],
                    evidence_ids=["E-000001", "E-000002"],
                )
            ],
        )
        receipt = await analysis.publish_structured(
            context(), candidate, "structured-call-1"
        )
        assert receipt == await analysis.publish_structured(
            context(), candidate, "structured-call-1"
        )
        state = await store.read_control(context())
        assert state.artifacts["analysis"].model_dump() == receipt["artifact"]
        assert "analysis" not in state.approved
        assert state.role_data["objects"][receipt["analysis_id"]]["payload"][
            "metric_ids"
        ] == [metric["metric_id"]]
    finally:
        active_dispatch.reset(token)
