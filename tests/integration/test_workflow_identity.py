from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.business import ControlState
from research_copilot.runtime.control import RunControl
from research_copilot.sandbox.config import SandboxConfig
from research_copilot.workspace import bundled_resources
from tests.unit.test_orchestration import ScriptModel, context


async def test_new_session_persists_explicit_workflow_at_creation(role_env):
    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.runtime.session import SessionRecord
    from research_copilot.contracts import RunContext

    store = role_env[2]
    await store.create(
        SessionRecord("new", "actor-1", "v2", "policy-1", "new"),
        TokenBudget(limit=1000, budget_id="new").snapshot(),
        workflow_version="products-v2",
    )
    state = await store.read_control(
        RunContext("actor-1", "new", "", "new", "policy-1")
    )
    assert state.role_data["workflow_version"] == "products-v2"


@pytest.mark.parametrize("status", ["created", "failed"])
async def test_workflow_is_frozen_before_first_graph_configuration_failure(
    role_env, tmp_path, monkeypatch, status
):
    services, business, store = role_env

    def reset(state):
        empty = ControlState()
        state.artifacts = empty.artifacts
        state.plans = []
        state.approved = {}
        state.role_data = {"workflow_version": "products-v2"}

    await store.mutate_control(context(), reset)
    record = replace(await store.load("report-1", "actor-1"), status=status)
    factory = SupervisorFactory(
        models={
            name: ScriptModel(script=[AIMessage(content="unused")])
            for name in ("supervisor", "searcher", "analysist", "writer", "reviewer")
        },
        store=store,
        business=business,
        checkpointer=InMemorySaver(),
        role_services=services,
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
    )
    original = SandboxConfig.load

    def fail():
        raise ValueError("injected initial config failure")

    monkeypatch.setattr(SandboxConfig, "load", fail)
    with pytest.raises(ValueError, match="injected"):
        await factory(record, RunControl())
    assert (await store.read_control(context())).role_data[
        "workflow_version"
    ] == "products-v2"
    monkeypatch.setattr(SandboxConfig, "load", original)
    await factory(replace(record, status="failed"), RunControl())
    assert (await store.read_control(context())).role_data[
        "workflow_version"
    ] == "products-v2"


async def test_old_source_contract_blocks_writes_but_keeps_history_readable(
    role_env, tmp_path
):
    services, business, store = role_env

    def freeze(state):
        state.role_data["runtime_manifest"] = {
            "source_contract": {"sha256": "a" * 64, "normalization_version": 1}
        }

    await store.mutate_control(context(), freeze)
    factory = SupervisorFactory(
        models={
            name: ScriptModel(script=[AIMessage(content="unused")])
            for name in ("supervisor", "searcher", "analysist", "writer", "reviewer")
        },
        store=store,
        business=business,
        checkpointer=InMemorySaver(),
        role_services=services,
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
        source_contract={"sha256": "b" * 64, "normalization_version": 2},
    )
    record = await store.load("report-1", "actor-1")
    with pytest.raises(PermissionError, match="source contract"):
        await factory(record, RunControl())
    await factory.for_status(record, RunControl())


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("advance_run", [False, True])
async def test_status_graph_does_not_select_or_persist_legacy_workflow(
    role_env, tmp_path, active, advance_run
):
    services, business, store = role_env
    factory = SupervisorFactory(
        models={
            name: ScriptModel(script=[AIMessage(content="unused")])
            for name in ("supervisor", "searcher", "analysist", "writer", "reviewer")
        },
        store=store,
        business=business,
        checkpointer=InMemorySaver(),
        role_services=services,
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
    )
    record = await store.load("report-1", "actor-1")
    active_graph = await factory(record, RunControl()) if active else None
    read_context = context()
    if advance_run:
        # Status callers can hold an earlier record while a worker starts a run.
        await store.save(replace(record, run_id="next-run", status="running"))
        read_context = replace(read_context, run_id="next-run")
    original_state = (await store.read_control(read_context)).model_dump()
    original_sandboxes = dict(factory._sandbox_services)
    before = {
        path.relative_to(services.workspace_root)
        for path in services.workspace_root.rglob("*")
    }
    original_services = dict(services.__dict__)
    original_business = dict(business.__dict__)
    await factory.for_status(record, RunControl())
    assert (await store.read_control(read_context)).model_dump() == original_state
    assert {
        path.relative_to(services.workspace_root)
        for path in services.workspace_root.rglob("*")
    } == before
    assert services.__dict__ == original_services
    assert business.__dict__ == original_business
    assert dict(factory._sandbox_services) == original_sandboxes
    assert (active_graph is not None) is active


async def test_factory_sandbox_checks_host_after_acquiring_slot(role_env, tmp_path):
    from research_copilot.runtime.maintenance import MaintenanceLost
    from research_copilot.runtime.runtime_watch import RuntimeWatch

    services, business, store = role_env

    async def probe():
        raise ConnectionError("Host lock connection lost before sandbox admission")

    watch = RuntimeWatch(probe)
    store.runtime_watch = watch
    factory = SupervisorFactory(
        models={
            name: ScriptModel(script=[AIMessage(content="unused")])
            for name in ("supervisor", "searcher", "analysist", "writer", "reviewer")
        },
        store=store,
        business=business,
        checkpointer=InMemorySaver(),
        role_services=services,
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
    )
    graph = await factory(await store.load("report-1", "actor-1"), RunControl())
    sandbox = next(iter(factory._sandbox_services.values()))
    try:
        with pytest.raises(MaintenanceLost):
            async with sandbox._locks("report-1", "dispatch-1"):
                pytest.fail("Sandbox admitted after its host connection was lost")
        assert watch.lost.is_set()
    finally:
        await watch.close()
    assert graph is not None


@pytest.fixture(autouse=True)
async def native_factory_identity(role_env):
    from research_copilot.runtime.autonomy import freeze_scope

    def native(state):
        state.role_data["workflow_version"] = "products-v2"
        freeze_scope(
            state,
            "report-1",
            {
                "report_type": "company_report",
                "objective": "research",
                "as_of": "2025-04-30",
            },
        )

    await role_env[2].mutate_control(context(), native)
