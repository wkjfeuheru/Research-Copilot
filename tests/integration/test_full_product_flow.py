"""Fresh Web product workflow: real host and Docker; frozen source/model boundaries.

No business results, approvals, evidence or checkpoints are pre-populated. Model
callbacks share only receipts returned by actual tools, never private store data.
This verifies orchestration and artifact integrity, not investment/model quality.
"""

import json
import os
from pathlib import Path
import re
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest
import psycopg
from psycopg import sql
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import RunContext, TaskBrief
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.business import BusinessCoordinator, ResearchPlan
from research_copilot.runtime.experience import ExperienceService
from research_copilot.runtime.session import SessionService
from research_copilot.runtime.storage import SqlSessionStore, postgres_runtime
from research_copilot.runtime.urls import to_checkpoint_url
from research_copilot.subagents.searcher import SourceTool
from research_copilot.subagents.services import RoleServices
from research_copilot.web.app import WorkbenchBackend, create_app
from research_copilot.web.charts import ChartService
from research_copilot.workspace import bundled_resources
from tests.integration.support import RespondingModel, tool_outputs
from tests.integration.test_web_review_flow import serve_test_app
from tests.unit.test_orchestration import call


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = """import os
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from growth import calculate
from charts import render
from io_contract import write_result
refs = REFS_PLACEHOLDER
items=[{'value':r['raw_value'],'context':{k:r[k] for k in ('entity','unit','currency','period','basis','scale')}} for r in refs]
metric=calculate('yoy',items[1],items[0])
pct=(Decimal(metric['value'])*100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
out=Path(os.environ['RC_OUTPUT_DIR'])
render(kind='bar',title='Revenue growth / 营收增长',unit='%',data={'labels':['AAA'],'series':[{'name':'Growth','values':[str(pct)]}]},output_dir=str(out/'growth'),engine='echarts',metric_keys=['growth'],alt='Growth is 20 percent')
write_result(out/'result.json',metrics=[{'key':'growth','value':str(pct),'unit':'%','input_keys':['revenue_2024','revenue_2023'],'semantic_type':'growth','formula_id':'growth_percent','numerator_basis':'revenue 2024','denominator_basis':'revenue 2023','display_precision':2,'is_estimate':False}],input_refs=refs,charts=['growth/chart.json'])
"""


class FrozenScenario:
    """Deterministic external boundaries with known synthetic usage on every call."""

    def __init__(self):
        self.facts = []
        self.execution = None
        self.analysis = None
        self.image_reads = 0
        self.source_calls = []
        tasks = [
            TaskBrief(
                task_id=task_id,
                role=role,
                objective="Frozen offline fixture: " + task_id,
                as_of="2025-04-30",
                required_fields=["revenue_2023", "revenue_2024"]
                if role == "searcher"
                else [],
                required_metrics=[
                    {"key": "growth", "semantic_type": "growth", "unit": "%"}
                ]
                if role == "analysist"
                else [],
                max_source_calls=2,
                max_executions=1,
                calculation_mode="custom",  # This fixture renders a chart as well as computing.
            )
            for task_id, role in (
                ("collect", "searcher"),
                ("analysis", "analysist"),
                ("write", "writer"),
                ("review", "reviewer"),
            )
        ]
        self.plan = ResearchPlan(
            report_type="company_report",
            objective="Frozen source integration fixture, not an investment report",
            tasks=tasks,
            analysis_execution_limit=1,
        )

    def model(self, respond, *, images=False):
        def metered(messages, step):
            response = respond(messages, step)
            response.usage_metadata = {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
            }
            return response

        return RespondingModel(
            script=[],
            respond=metered,
            profile={
                "max_input_tokens": 100000,
                "max_output_tokens": 1000,
                "image_inputs": images,
            },
        )

    async def fetch(self, args):
        self.source_calls.append(args.copy())
        return {
            "context": self.fact_context(args["period"]),
            "revenue": {"2023": "100", "2024": "120"}[args["period"]],
        }

    @staticmethod
    def fact_context(period):
        return {
            "entity": "AAA",
            "unit": "CNY",
            "currency": "CNY",
            "scale": "1",
            "period": period,
            "basis": "consolidated",
        }

    def supervisor(self, messages, step):
        if step == 0:
            return call(
                "set_report_scope",
                {
                    "scope": {
                        "report_type": self.plan.report_type,
                        "objective": self.plan.objective,
                        "as_of": self.plan.tasks[0].as_of,
                    }
                },
                "scope",
            )
        if step in {1, 7}:
            return call(
                "write_todos",
                {
                    "todos": [
                        {
                            "content": task.objective,
                            "status": "completed" if step == 7 else "in_progress",
                        }
                        for task in self.plan.tasks
                    ]
                },
                "todos-" + str(step),
            )
        if 2 <= step <= 5:
            task = self.plan.tasks[step - 2]
            return call(
                "task",
                {"subagent_type": task.role, "description": task.model_dump_json()},
                "dispatch-" + task.task_id,
            )
        if step == 6:
            return call(
                "signoff_report",
                {"artifact": tool_outputs(messages)["task"]["reviewed"]},
                "signoff",
            )
        assert step == 8
        return AIMessage(content="报告已签署。")

    def searcher(self, messages, step):
        if step:
            receipt = tool_outputs(messages)["sina_filing"]
            period = ("2023", "2024")[step - 1]
            raw = {"2023": "100", "2024": "120"}[period]
            self.facts.append(
                {
                    "key": "revenue_" + period,
                    "evidence_id": receipt["evidence_id"],
                    "content_hash": receipt["content_hash"],
                    "pointer": "/revenue",
                    "quote": raw,
                    "raw_value": raw,
                    **self.fact_context(period),
                }
            )
        if step < 2:
            return call(
                "sina_filing", {"period": ("2023", "2024")[step]}, f"fetch-{step}"
            )
        assert step == 2
        return call(
            "SearchResult",
            {
                "task_id": "collect",
                "status": "completed",
                "summary": "Frozen filings acquired through host source tools",
                "evidence_ids": self.evidence_ids,
                "coverage": [f["key"] for f in self.facts],
                "facts": self.facts,
                "stop_reason": "required_complete",
                "gaps": [],
            },
            "search-result",
        )

    @property
    def evidence_ids(self):
        return [fact["evidence_id"] for fact in self.facts]

    def analysist(self, messages, step):
        envelope = json.loads(
            next(m.content for m in messages if isinstance(m, HumanMessage))
        )
        prefix = envelope["write_prefix"]
        if step == 0:
            refs = [
                {k: v for k, v in fact.items() if k != "quote"} for fact in self.facts
            ]
            return call(
                "write_file",
                {
                    "file_path": prefix + "run.py",
                    "content": SCRIPT.replace("REFS_PLACEHOLDER", repr(refs)),
                },
                "code",
            )
        if step == 1:
            return call("execute", {"command": f"python {prefix}run.py"}, "execute")
        if step == 2:
            output = next(
                m.content
                for m in reversed(messages)
                if isinstance(m, ToolMessage) and m.name == "execute"
            )
            self.execution = json.JSONDecoder().raw_decode(output)[0]
            assert self.execution["status"] == "registered", self.execution
            return call(
                "write_file",
                {
                    "file_path": prefix + "analysis.json",
                    "content": json.dumps(
                        {
                            "summary": "Frozen revenue growth",
                            "conclusions": ["Revenue increased 20 percent."],
                            "evidence_ids": self.evidence_ids,
                            "metric_ids": self.execution["metric_ids"],
                            "chart_ids": self.execution["chart_ids"],
                            "assumptions": [],
                            "limitations": [],
                            "claims": [
                                {
                                    "claim_id": "growth",
                                    "text": "Revenue increased 20 percent.",
                                    "metric_ids": self.execution["metric_ids"],
                                    "evidence_ids": self.evidence_ids,
                                    "required_in_report": True,
                                }
                            ],
                        }
                    ),
                },
                "candidate",
            )
        if step == 3:
            return call(
                "save_analysis", {"candidate_path": prefix + "analysis.json"}, "save"
            )
        assert step == 4
        self.analysis = tool_outputs(messages)["save_analysis"]
        return call(
            "AnalysisResult",
            {
                "task_id": "analysis",
                "status": "completed",
                "summary": "Real sandbox result saved",
                "analysis_id": self.analysis["analysis_id"],
                "artifact_refs": [self.analysis["artifact"]],
                "evidence_ids": self.evidence_ids,
                "metric_ids": self.execution["metric_ids"],
                "chart_ids": self.execution["chart_ids"],
                "gaps": [],
            },
            "analysis-result",
        )

    def writer(self, messages, step):
        if step == 0:
            return call(
                "get_analysis",
                {"analysis_id": self.analysis["analysis_id"]},
                "read-analysis",
            )
        assert step == 1
        assert tool_outputs(messages)["get_analysis"]
        template = (
            bundled_resources() / "skills/writer/reports/references/company_report.md"
        ).read_text("utf-8")
        sections = [
            {
                "heading": heading,
                "text": "Source-supported observations and limitations. " * 9
                + "[^E-000001]",
                "claims": [],
            }
            for heading in re.findall(r"^## (.+)$", template, re.MULTILINE)
        ]
        quote = "Revenue increased 20 percent."
        sections[0]["text"] += (
            "\n\n" + quote + f"\n\n![Growth](chart:{self.execution['chart_ids'][0]})"
        )
        sections[0]["claims"] = [
            {
                "quote": quote,
                "metric_ids": self.execution["metric_ids"],
                "evidence_ids": self.evidence_ids,
                "conclusion_id": "growth",
            }
        ]
        return call(
            "WritingResult",
            {
                "task_id": "write",
                "status": "completed",
                "summary": "Frozen fixture draft",
                "report_type": "company_report",
                "analysis_ids": [self.analysis["analysis_id"]],
                "evidence_ids": self.evidence_ids,
                "title": "离线全链路夹具：不构成真实研究",
                "sections": sections,
                "gaps": [],
            },
            "writing-result",
        )

    def reviewer(self, messages, step):
        payload = json.loads(
            next(m.content for m in messages if isinstance(m, HumanMessage))
        )
        assert "correction" not in payload, payload
        unread = [
            target
            for target in payload["targets"]
            if "delivered_material" not in payload
            or target["tool"] == "read_snapshot_chart"
        ]
        if "delivered_material" in payload and unread:
            assert "read_snapshot_chart" in payload["instruction"], (
                "Inline text instructions must still require reading frozen images"
            )
        if unread and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": target["tool"],
                        "args": {"target_id": target["target_id"]},
                        "id": f"read-{step}-{i}",
                    }
                    for i, target in enumerate(unread)
                ],
            )
        if "data:image/png;base64," in json.dumps([m.content for m in messages]):
            self.image_reads += 1
        return call(
            "BranchReview",
            {
                "pass_confidence": 0.99,
                "checks_complete": True,
                "covered_targets": [t["target_id"] for t in payload["targets"]],
                "issues": [
                    {
                        "category": "reasoning",
                        "severity": "major",
                        "target_id": "document:0",
                        "location": "Revenue",
                        "quote": "Source-supported",
                        "basis": self.evidence_ids[0],
                        "reason": "Check scope",
                        "suggestion": "Clarify claim",
                    }
                ],
            },
            f"review-result-{step}",
        )


def full_backend(store, saver, workspace):
    scenario = FrozenScenario()
    business = BusinessCoordinator(store)
    services = RoleServices(
        repository=store,
        workspace_root=workspace,
        business=business,
        report_rules={
            "version": 1,
            "sensitive_words": [],
            "prohibited_patterns": [],
            "required_disclosures": ["risk_warning", "disclaimer"],
            "risk_warning_template": "Data and methodology risks apply.",
            "disclaimer_template": "For research reference only.",
            "is_example": False,
        },
    )
    services.experience = ExperienceService(store)
    models = {
        role: scenario.model(
            getattr(scenario, role),
            images=role == "reviewer",
        )
        for role in ("supervisor", "searcher", "analysist", "writer", "reviewer")
    }
    factory = SupervisorFactory(
        models=models,
        store=store,
        business=business,
        checkpointer=saver,
        workspace_root=workspace,
        resources_root=bundled_resources(),
        role_services=services,
        source_tools=[
            SourceTool(
                "sina_filing",
                "sina_finance",
                "filing",
                "Read frozen filing fixture",
                {
                    "type": "object",
                    "properties": {
                        "period": {"type": "string", "enum": ["2023", "2024"]}
                    },
                    "required": ["period"],
                    "additionalProperties": False,
                },
                scenario.fetch,
            )
        ],
    )
    store.resume_validator = business.validate_resume
    session = SessionService(
        store=store, agent_factory=factory, policy_version="policy-1"
    )
    backend = WorkbenchBackend(
        session=session,
        store=store,
        actor_id="actor-1",
        workspace_root=workspace,
        chart_service=ChartService(services),
        experience=services.experience,
        policy_version="policy-1",
        execution_policy=ROOT / "config/execution_policy.toml",
    )
    return backend, scenario, models


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.db)])
async def fresh_product_environment(request, tmp_path):
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
        store = SqlSessionStore(engine)
        await store.setup()
        try:
            yield store, InMemorySaver()
        finally:
            await engine.dispose()
        return
    base = os.environ.get("RC_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Requires explicit isolated RC_TEST_DATABASE_URL")
    base = to_checkpoint_url(base)
    params = psycopg.conninfo.conninfo_to_dict(base)
    name = "rc_fulltest_" + uuid4().hex
    url = URL.create(
        "postgresql",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", 5432)),
        database=name,
    ).render_as_string(hide_password=False)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            async with postgres_runtime(url, initialize=True) as runtime:
                yield runtime
        finally:
            assert name.startswith("rc_fulltest_") and name != params.get("dbname")
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.mark.docker
async def test_fresh_product_workflow_through_web_and_real_sandbox(
    fresh_product_environment, tmp_path
):
    store, saver = fresh_product_environment
    backend, scenario, models = full_backend(store, saver, tmp_path / "workspace")
    try:
        async with serve_test_app(create_app(backend=backend)) as url:
            async with httpx.AsyncClient(base_url=url, timeout=15) as client:
                response = await client.post(
                    "/api/reports",
                    json={
                        "user_request": "离线全链路夹具：分析冻结收入同比变化，不使用真实模型或在线资料"
                    },
                )
                assert response.status_code == 201, response.text
                report_id = response.json()["report"]["id"]
                path = f"/api/reports/{report_id}"

                async def status():
                    await backend.runs.drain()
                    result = await client.get(path)
                    assert result.status_code == 200, result.text
                    return result.json()

                async def approve(gate, decision=None):
                    result = await client.post(
                        path + "/decisions",
                        json={
                            "decisions": {
                                gate["id"]: decision
                                or {"decisions": [{"type": "approve"}]}
                            }
                        },
                    )
                    assert result.status_code == 200, result.text
                    return await status()

                current = await status()
                assert current["workflow_version"] == "products-v2"
                assert not current["plan"]
                assert len(scenario.source_calls) == 2
                gate = current["interrupts"][0]
                assert gate["kind"] == "reviewer_deep_review", current
                review = gate["review"]
                assert review["checks_complete"] and review["issues"], review
                assert scenario.image_reads > 0, (
                    "Reviewer must receive actual rendered PNG"
                )
                assert not current["signed"] and not current["signoff_ready"]
                current = await approve(
                    gate,
                    {
                        "decision": "approve",
                        "comment": "Frozen fixture scope checked",
                        "snapshot_id": review["snapshot_id"],
                        "review_session_id": review["review_session_id"],
                        "issue_dispositions": [
                            {
                                "issue_id": issue["issue_id"],
                                "decision": "false_positive",
                                "reason": "Verified frozen scope, numeric lineage and limitations in this fixture",
                            }
                            for issue in review["issues"]
                        ],
                    },
                )
                assert current["signoff_ready"] and not current["signed"], current
                assert current["interrupts"][0]["tool"] == "signoff_report"
                current = await approve(current["interrupts"][0])
                assert current["signed"] and not current["interrupts"], current
                record = await store.load(report_id, "actor-1")
                context = RunContext(
                    record.actor_id,
                    report_id,
                    record.run_id,
                    record.budget_id,
                    record.policy_version,
                )
                state = await store.read_control(context)
                assert state.role_data["workflow_version"] == "products-v2"
                assert len(state.results) == 4
                assert len(state.role_data["executions"]) == 1
                if store.sandbox_slots is not None:
                    from sqlalchemy import select
                    from research_copilot.runtime.sandbox_slots import claims

                    async with store.engine.connect() as connection:
                        capacity = (
                            (
                                await connection.execute(
                                    select(claims).where(
                                        claims.c.report_id == report_id,
                                    )
                                )
                            )
                            .mappings()
                            .all()
                        )
                    assert len(capacity) == 1 and capacity[0]["state"] == "released"
                    assert capacity[0]["execution_id"] in state.role_data["executions"]
                assert len(await store.experience.jobs(report_id)) == 1
                document = (await client.get(path + "/report")).json()
                assert (
                    document["is_frozen"]
                    and "Revenue increased 20 percent." in document["content"]
                )
                chart = await backend.chart_service.resolve(
                    "actor-1",
                    report_id,
                    str(state.artifacts["report"].version),
                    scenario.execution["chart_ids"][0],
                )
                assert chart["receipt"]["engine"] == "echarts"
                assert chart["assets"][chart["paths"]["png"]].startswith(
                    b"\x89PNG\r\n\x1a\n"
                )
                assert models["supervisor"].position == 9
    finally:
        await backend.runs.aclose()
