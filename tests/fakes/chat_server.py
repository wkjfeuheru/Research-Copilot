"""Disposable browser fixture: real HTTP/SSE/SQLite/checkpoints; scripted research.

No external provider/model, real financial conclusion, or user database is used.
Run: uv run --frozen python -m tests.fakes.chat_server
"""

import asyncio
import hashlib
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired

import uvicorn
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.contracts import ArtifactRef, RunContext, TaskBrief
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.business import (
    BusinessCoordinator,
    ResearchPlan,
    approval_key,
)
from research_copilot.runtime.conversation import public_error
from research_copilot.runtime.loop import serve_loop
from research_copilot.runtime.session import SessionRecord, SessionService
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.runtime.workflow import (
    WorkflowBlocked,
    decide_next,
    dispatch_id,
)
from research_copilot.web.app import WorkbenchBackend, create_app


class State(MessagesState):
    planned: NotRequired[bool]
    declined: NotRequired[bool]


def build_graph(store, business):
    async def talk(state, runtime: Runtime[RunContext]):
        await store.mutate_control(
            runtime.context,
            lambda s: s.role_data.update(workflow_version="products-v1"),
        )
        text = next(m.content for m in reversed(state["messages"]) if m.type == "human")
        if "2025" not in text:
            return {
                "messages": [
                    AIMessage(
                        content="你希望比较哪两个季度？请补充具体期间，我再整理研究计划。",
                        additional_kwargs={"awaiting_user_input": True},
                    )
                ],
                "planned": False,
            }
        tasks = [
            TaskBrief(
                task_id=role,
                role=role,
                objective="浏览器验收夹具：" + role,
                as_of="2026-10-31",
            )
            for role in ["searcher", "analysist", "writer", "reviewer"]
        ]
        await business.propose_plan(
            runtime.context,
            ResearchPlan(
                report_type="flash_comment",
                objective="比较2025Q1和2025Q2的毛利率变化（离线交互夹具）",
                tasks=tasks,
            ),
        )
        return {
            "messages": [
                AIMessage(
                    content="我已整理研究计划。请确认范围后，我会继续采集与分析。"
                )
            ],
            "planned": True,
        }

    def gate(kind):
        async def confirm(state, runtime: Runtime[RunContext]):
            control = await store.read_control(runtime.context)
            ref = control.artifacts[kind]
            name = {
                "plan": "confirm_plan",
                "analysis": "confirm_analysis",
                "report": "signoff_report",
            }[kind]
            # No writes or model calls are performed before the restartable interrupt.
            value = interrupt(
                {
                    "action_requests": [
                        {"name": name, "args": {"artifact": ref.model_dump()}}
                    ]
                }
            )
            if value["decisions"][0]["type"] != "approve":
                return {
                    "declined": True,
                    "messages": [
                        AIMessage(
                            content="已记录你的拒绝决定。请新建对话调整研究范围。"
                        )
                    ],
                }
            await business.confirm(
                runtime.context,
                name,
                ref,
                approval_key(runtime.context.report_id, name, ref),
            )
            return {}

        return confirm

    def finish(state, report_id):
        action = decide_next(state, report_id)
        if action.get("name") == "task":
            state.results[dispatch_id(report_id, action["id"])] = {
                "status": "passed"
                if action["args"]["subagent_type"] == "reviewer"
                else "completed"
            }

    async def analysis(state, runtime: Runtime[RunContext]):
        async def progress(role):
            await store.mutate_control(
                runtime.context,
                lambda s: s.role_data.setdefault("model_audit", []).append(
                    {"request_id": "fixture-" + role, "role": role, "event": "started"}
                ),
            )
            await asyncio.sleep(0.7)

        await progress("searcher")
        await progress("analysist")

        def save(s):
            finish(s, runtime.context.report_id)
            finish(s, runtime.context.report_id)
            ref = ArtifactRef(
                artifact_id="fixture-analysis", version="1", content_hash="b" * 64
            )
            payload = {
                "summary": "浏览器夹具：材料已组织，分析等待确认。",
                "conclusions": ["这是交互测试数据，不是投资结论。"],
                "assumptions": [],
                "limitations": ["未调用真实数据或模型。"],
                "metric_ids": [],
                "chart_ids": [],
                "evidence_ids": ["E-000001"],
            }
            s.artifacts["analysis"] = ref
            s.role_data.setdefault("objects", {})[ref.artifact_id] = {
                "kind": "analysis",
                "ref": ref.model_dump(),
                "payload": payload,
            }
            s.role_data["evidence"] = {
                "E-000001": {
                    "evidence_id": "E-000001",
                    "raw": {
                        "title": "交互验收材料",
                        "body": "离线浏览器夹具材料，用于验证来源展示。",
                    },
                    "source": {"server": "fixture", "tool": "offline"},
                    "content_hash": "c" * 64,
                    "kind": "web",
                }
            }

        await store.mutate_control(runtime.context, save)
        return {
            "messages": [
                AIMessage(content="材料已整理，分析结果已保存。请核对分析结论和限制。")
            ]
        }

    async def report(state, runtime: Runtime[RunContext]):
        await store.mutate_control(
            runtime.context,
            lambda s: s.role_data["model_audit"].append(
                {"request_id": "fixture-writer", "role": "writer", "event": "started"}
            ),
        )
        await asyncio.sleep(0.7)
        text = (
            "# 研究结论\n\n> 离线交互验收夹具，不代表真实公司财务数据。\n\n## 结论与依据\n\n这是由已提交报告通过 SSE 发送的正文。来源、假设和限制应分别说明。[^E-000001]\n\n"
            + ("研究过程应可追溯，模型运行成功不等于结论已经通过审核。\n\n" * 70)
            + "## 研究限制\n\n本页未使用付费模型或实时数据。\n\n[^E-000001]: 交互验收材料。"
        )

        def save(s):
            finish(s, runtime.context.report_id)
            ref = ArtifactRef(
                artifact_id="fixture-report",
                version="1",
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
            s.artifacts["report"] = ref
            s.role_data["objects"][ref.artifact_id] = {
                "kind": "report",
                "ref": ref.model_dump(),
                "payload": {
                    "document": text,
                    "analysis_ref": s.artifacts["analysis"].model_dump(),
                },
            }
            finish(s, runtime.context.report_id)
            s.reviewed_report = ref
            s.role_data["reviews"] = {
                "fixture-review": {
                    "review_id": "fixture-review",
                    "reviewed": ref.model_dump(),
                    "outcome": "passed",
                    "checks_complete": True,
                    "pass_confidence": 0.9,
                    "issues": [],
                }
            }

        await store.mutate_control(runtime.context, save)
        return {
            "messages": [
                AIMessage(
                    content="报告草稿已提交，审核夹具已完成检查。请阅读正文后决定是否签署。"
                )
            ]
        }

    graph = StateGraph(State, context_schema=RunContext)
    graph.add_node("talk", talk)
    graph.add_node("plan", gate("plan"))
    graph.add_node("analysis", analysis)
    graph.add_node("confirm_analysis", gate("analysis"))
    graph.add_node("report", report)
    graph.add_node("signoff", gate("report"))
    graph.add_edge(START, "talk")
    graph.add_conditional_edges("talk", lambda s: "plan" if s.get("planned") else END)
    for a, b in [("plan", "analysis"), ("confirm_analysis", "report")]:
        graph.add_conditional_edges(
            a, lambda s, target=b: END if s.get("declined") else target
        )
    for a, b in [
        ("analysis", "confirm_analysis"),
        ("report", "signoff"),
        ("signoff", END),
    ]:
        graph.add_edge(a, b)
    return graph.compile(checkpointer=InMemorySaver())


@asynccontextmanager
async def lifespan(app):
    with tempfile.TemporaryDirectory(prefix="rc-chat-preview-") as directory:
        root = Path(directory)
        engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'preview.db'}")
        store = SqlSessionStore(engine)
        await store.setup()
        business = BusinessCoordinator(store)
        store.resume_validator = business.validate_resume
        graph = build_graph(store, business)
        session = SessionService(
            store=store,
            agent_factory=lambda record, control: graph,
            policy_version="v2-policy-1",
        )
        app.state.backend = WorkbenchBackend(
            session=session, store=store, actor_id="local", workspace_root=root
        )
        await store.create(
            SessionRecord(
                "blocked-demo",
                "local",
                "v2",
                "v2-policy-1",
                "blocked-demo",
                run_id="failed-run",
                status="failed",
                user_request="宁德时代的毛利率环比下滑，碳酸锂涨价和储能占比各贡献多少？",
                created_at=datetime.now(UTC).isoformat(),
            ),
            TokenBudget(limit=2100000, budget_id="blocked-demo").snapshot(),
        )
        context = RunContext(
            "local", "blocked-demo", "failed-run", "blocked-demo", "v2-policy-1"
        )
        await store.mutate_control(
            context,
            lambda s: s.role_data.update(
                workflow_version="products-v1",
                last_run_error={
                    "code": "WorkflowBlocked",
                    "message": public_error(
                        WorkflowBlocked("Planner returned no plan proposal")
                    ),
                },
                evidence={
                    "E-000001": {
                        "kind": "financial_data",
                        "content_hash": "a" * 64,
                        "collected_at": "2026-01-02T03:04:05+00:00",
                        "raw": {
                            "title": "交互验收材料",
                            "structured": {
                                "available": True,
                                "domain": "fundamentals",
                                "items": [{"report_date": "2025-12-31"}],
                            },
                        },
                        "source": {
                            "server": "financekb_fundamentals",
                            "tool": "get_financial_statements",
                        },
                    },
                    "E-000002": {
                        "kind": "search_snippet",
                        "content_hash": "d" * 64,
                        "collected_at": "2026-01-02T03:05:00+00:00",
                        "raw": {
                            "title": "示例券商研报片段",
                            "body": "知识库命中片段：2025Q2 毛利率环比改善。",
                            "source_ref": {"collection": "research"},
                            "structured": {
                                "page": 12,
                                "heading_path": "第二章/盈利预测",
                                "broker": "示例证券",
                                "report_type": "company",
                                "date": "2025-08-30",
                            },
                        },
                        "source": {
                            "server": "financekb_knowledge",
                            "tool": "search_research",
                        },
                    },
                    "E-000003": {
                        "kind": "web",
                        "content_hash": "e" * 64,
                        "collected_at": "2025-01-01T00:00:00+00:00",
                        "raw": {
                            "title": "历史来源记录",
                            "body": "旧来源证据仍应可读。",
                        },
                        "source": {"server": "tavily", "tool": "search"},
                    },
                },
            ),
        )
        try:
            yield
        finally:
            await app.state.backend.runs.aclose()
            await engine.dispose()


app = create_app(
    lifespan=lifespan,
    static_dir=(
        None
        if os.environ.get("RC_FIXTURE_STATIC") == "off"
        else Path(__file__).parents[2] / "frontend/dist"
    ),
)
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5187, loop=serve_loop, log_level="warning")
