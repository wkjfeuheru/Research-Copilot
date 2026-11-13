import json
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.tools import tool, ToolRuntime
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from research_copilot.contracts import (
    RunContext,
    ArtifactRef,
    Dispatch,
    RoleResult,
    ReviewResult,
    CONFIRMATION_TOOLS,
)
from research_copilot.runtime.business import ResearchPlan
from research_copilot.runtime.scheduler import ProductWorkflowMiddleware
from research_copilot.runtime.workflow import dispatch_id
from research_copilot.runtime.control import RunControl
from research_copilot.orchestration.tools import build_top_tools
from tests.unit.test_orchestration import ScriptModel, call, context, config
from tests.unit.test_product_workflow import state as fixture_state


@pytest.mark.asyncio
async def test_native_product_loop_resumes_all_gates_with_one_planner_call(role_env):
    _, business, store = role_env
    business.role_services = (
        None  # Offline product publishers replace role models only.
    )

    def reset(s):
        s.artifacts.clear()
        s.approved.clear()
        s.plans.clear()
        s.role_data["workflow_version"] = "products-v1"

    await store.mutate_control(context(), reset)
    tasks = fixture_state().plans[0]["plan"]["tasks"]
    plan = ResearchPlan(report_type="company_report", objective="research", tasks=tasks)
    parent = ScriptModel(
        script=[call("propose_plan", {"plan": plan.model_dump()}, "plan-call")]
    )
    invoked = []

    @tool
    async def task(
        description: str, subagent_type: str, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Publish an offline role product through the real coordinator."""
        ident = dispatch_id(runtime.context.report_id, runtime.tool_call_id)
        d = Dispatch(ident, subagent_type, f"/workspace/tasks/{ident}/")
        await business.authorize_dispatch(runtime.context, d)
        invoked.append(subagent_type)
        ref = ArtifactRef(artifact_id=subagent_type, version="1", content_hash="b" * 64)
        kind = {"analysist": "analysis", "writer": "report"}.get(subagent_type)
        if kind:

            def publish(s):
                s.artifacts[kind] = ref
                if kind == "report":
                    s.role_data.setdefault("objects", {})[ref.artifact_id] = {
                        "ref": ref.model_dump(),
                        "payload": {
                            "analysis_ref": s.artifacts["analysis"].model_dump()
                        },
                    }

            await store.mutate_control(runtime.context, publish)
        if subagent_type == "reviewer":
            s = await store.read_control(runtime.context)
            await store.mutate_control(
                runtime.context,
                lambda current: current.role_data.setdefault("reviews", {}).update(
                    {
                        "offline-review": {
                            "reviewed": s.artifacts["report"].model_dump(),
                            "outcome": "passed",
                            "checks_complete": True,
                            "pass_confidence": 0.99,
                            "issues": [],
                        },
                    }
                ),
            )
            result = ReviewResult(
                status="passed",
                summary="offline complete",
                reviewed=s.artifacts["report"],
                issues=[],
                comments=[],
            )
        else:
            result = RoleResult(
                role=subagent_type,
                status="completed",
                summary="offline product",
                references=[ref] if kind else [],
                gaps=[],
            )
        await business.accept_result(runtime.context, d, result)
        return result.model_dump()

    budget = await store.load_budget(context())
    agent = create_agent(
        parent,
        tools=[*build_top_tools(business), task],
        context_schema=RunContext,
        middleware=[
            ProductWorkflowMiddleware(business, RunControl(), budget),
            HumanInTheLoopMiddleware(
                interrupt_on={n: True for n in CONFIRMATION_TOOLS}
            ),
        ],
        checkpointer=InMemorySaver(),
    )
    value = {"messages": [("user", "research")]}
    gates = []
    for _ in range(4):
        result = await agent.ainvoke(value, config(), context=context())
        interrupts = result.get("__interrupt__", ())
        if not interrupts:
            break
        pending = {i.id: i.value for i in interrupts}
        gates.extend(a["name"] for p in pending.values() for a in p["action_requests"])
        decisions = {
            i: {"decisions": [{"type": "approve"} for a in p["action_requests"]]}
            for i, p in pending.items()
        }
        await business.validate_resume(context(), pending, decisions)
        value = Command(resume=decisions)
    assert gates == ["confirm_plan", "confirm_analysis", "signoff_report"]
    assert invoked == ["searcher", "analysist", "writer", "reviewer"]
    assert parent.position == 1
    s = await store.read_control(context())
    assert s.approved["report"] == s.artifacts["report"]
    assert (await business.get_status(context()))["workflow"]["phase"] == "complete"


@pytest.mark.asyncio
async def test_committed_role_receipt_replay_does_not_reexecute():
    from types import SimpleNamespace
    from research_copilot.orchestration.middleware import DispatchMiddleware
    from research_copilot.runtime.business import ControlState

    ident = dispatch_id("report-1", "stable-call")
    s = ControlState(
        dispatches={ident: {"role": "searcher", "inputs": {}}},
        results={
            ident: {
                "role": "searcher",
                "status": "completed",
                "summary": "saved",
                "references": [],
                "gaps": [],
            }
        },
        role_data={"scopes": {ident: {"description": "frozen task"}}},
    )

    class Repo:
        async def read_control(self, context):
            return s

    class Business:
        repository = Repo()

        async def authorize_dispatch(self, *args):
            raise AssertionError("No second authorization/execution")

    req = SimpleNamespace(
        runtime=SimpleNamespace(context=context()),
        tool_call={
            "id": "stable-call",
            "name": "task",
            "args": {"subagent_type": "searcher", "description": "frozen task"},
        },
    )

    async def forbidden(request):
        raise AssertionError("No second role execution")

    result = await DispatchMiddleware(
        Business(), RunControl(), "report-1"
    ).awrap_tool_call(req, forbidden)
    assert json.loads(result.content)["summary"] == "saved"


@pytest.mark.asyncio
async def test_production_factory_uses_native_scope_and_explicit_clarification(
    role_env, tmp_path
):
    from research_copilot.orchestration.factory import SupervisorFactory
    from research_copilot.runtime.session import SessionRecord
    from research_copilot.workspace import bundled_resources

    _, business, store = role_env

    def reset(s):
        s.artifacts.clear()
        s.approved.clear()
        s.plans.clear()
        s.role_data.clear()
        s.role_data["workflow_version"] = "products-v2"

    await store.mutate_control(context(), reset)
    plan = ResearchPlan(
        report_type="company_report",
        objective="research",
        tasks=fixture_state().plans[0]["plan"]["tasks"],
    )
    models = {
        role: ScriptModel(
            script=[
                call(
                    "set_report_scope",
                    {
                        "scope": {
                            "report_type": plan.report_type,
                            "objective": plan.objective,
                            "as_of": "2025-04-30",
                        }
                    },
                    "scope",
                ),
                call("ask_user", {"question": "Which entity?"}, "ask"),
            ]
        )
        for role in ("supervisor", "searcher", "analysist", "writer", "reviewer")
    }
    for response in models["supervisor"].script:
        response.usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        }
    factory = SupervisorFactory(
        models=models,
        store=store,
        business=business,
        checkpointer=InMemorySaver(),
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
    )
    agent = await factory(
        SessionRecord(
            "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
        ),
        RunControl(),
    )
    result = await agent.ainvoke(
        {"messages": [("user", "research")]}, config(), context=context()
    )
    assert result["__interrupt__"][0].value["kind"] == "clarification"
    assert models["supervisor"].position == 2
    assert all(
        models[r].position == 0 for r in ("searcher", "analysist", "writer", "reviewer")
    )
    s = await store.read_control(context())
    assert s.role_data["workflow_version"] == "products-v2"
    assert not s.plans and not s.approved
    assert s.role_data["report_scope"]["report_type"] == "company_report"


@pytest.mark.asyncio
async def test_proposal_rejects_a_calculated_output_as_required_collection_fact(
    role_env,
):
    from research_copilot.runtime.business import ResearchPlan
    from tests.unit.test_product_workflow import state
    from tests.unit.test_orchestration import context

    _, business, store = role_env
    await store.mutate_control(
        context(), lambda s: s.role_data.update(workflow_version="products-v1")
    )
    tasks = state().plans[0]["plan"]["tasks"]
    tasks[0]["required_fields"] = ["revenue_q1", "gross_margin_q2"]
    tasks[1]["required_fields"] = ["gross_margin_q2"]
    plan = ResearchPlan(
        report_type="flash_comment", objective="计算单季毛利率", tasks=tasks
    )
    with pytest.raises(ValueError, match="gross_margin_q2"):
        await business.propose_plan(context(), plan)
    tasks[0]["required_fields"] = ["revenue_q1", "cost_q1", "revenue_h1", "cost_h1"]
    approved_candidate = await business.propose_plan(
        context(),
        ResearchPlan(
            report_type="flash_comment", objective=plan.objective, tasks=tasks
        ),
    )
    assert approved_candidate.artifact_id


@pytest.mark.asyncio
async def test_conflicting_plan_returns_repair_feedback_without_saving(role_env):
    from types import SimpleNamespace

    _, business, store = role_env
    await store.mutate_control(
        context(), lambda s: s.role_data.update(workflow_version="products-v1")
    )
    before = (await store.read_control(context())).plans
    tasks = fixture_state().plans[0]["plan"]["tasks"]
    tasks[0]["required_fields"] = ["gross_margin_q2"]
    tasks[1]["required_metrics"] = [
        {"key": "gross_margin_q2", "semantic_type": "毛利率", "unit": "%"}
    ]
    plan = ResearchPlan(report_type="flash_comment", objective="毛利率", tasks=tasks)
    proposer = next(t for t in build_top_tools(business) if t.name == "propose_plan")
    feedback = await proposer.coroutine(plan, SimpleNamespace(context=context()))
    assert feedback["error"] == "collection_requires_derived_output"
    assert "gross_margin_q2" in feedback["message"]
    assert (await store.read_control(context())).plans == before
