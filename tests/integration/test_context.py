"""P0 factory wiring with real repositories; no provider or Docker requests."""

from types import SimpleNamespace
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.control import RunControl
from research_copilot.workspace import bundled_resources
from tests.unit.test_orchestration import ScriptModel, context
from tests.integration.test_searcher import dispatch


@pytest.mark.asyncio
async def test_factory_shares_summary_budget_and_freezes_summary_configuration(
    role_env, monkeypatch
):
    services, business, store = role_env
    models = {
        name: ScriptModel(script=[AIMessage(content="unused")])
        for name in (
            "supervisor",
            "searcher",
            "analysist",
            "writer",
            "reviewer",
            "summarizer",
        )
    }
    models["summarizer"].model_name = "independent-summary"
    captured = {}

    def assemble(**kwargs):
        captured.update(kwargs)
        return "compiled"

    monkeypatch.setattr(
        "research_copilot.orchestration.factory.build_supervisor", assemble
    )
    factory = SupervisorFactory(
        models,
        store,
        business,
        InMemorySaver(),
        services.workspace_root,
        bundled_resources(),
        role_services=services,
    )
    ctx = context()
    await store.mutate_control(
        ctx, lambda s: s.role_data.update(workflow_version="products-v2")
    )
    from research_copilot.runtime.autonomy import ReportScope

    await business.set_report_scope(
        ctx,
        ReportScope(
            report_type="company_report", objective="research", as_of="2025-04-30"
        ),
    )
    record = SimpleNamespace(
        **{
            name: getattr(ctx, name)
            for name in (
                "actor_id",
                "report_id",
                "run_id",
                "budget_id",
                "policy_version",
            )
        }
    )
    assert await factory(record, RunControl()) == "compiled"
    assert captured["summary_model"].budget is captured["model"].budget
    assert captured["summary_model"].inner is models["summarizer"]
    await dispatch(role_env)
    before = (await store.read_control(ctx)).role_data["runtime_manifest"]
    models["summarizer"].model_name = "different-summary"
    await factory(record, RunControl())
    with pytest.raises(ValueError, match="configuration"):
        await dispatch(
            role_env,
            ident="after-change",
            description='{"task_id":"after-change","role":"searcher","objective":"research","as_of":"2025-04-30"}',
        )
    assert (await store.read_control(ctx)).role_data["runtime_manifest"] == before
