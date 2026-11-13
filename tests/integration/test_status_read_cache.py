"""Compiled status readers may be reused; checkpoint state must stay live."""

import cProfile
import asyncio
from dataclasses import replace
import json
import time

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.session import SessionService
from research_copilot.runtime.control import RunControl
from research_copilot.workspace import bundled_resources
from tests.unit.test_orchestration import ScriptModel, context


@pytest.fixture
def factory(role_env, tmp_path):
    services, business, store = role_env
    return SupervisorFactory(
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


@pytest.mark.parametrize("version", ["products-v2"])
async def test_repeated_status_queries_do_not_recompile(
    factory, monkeypatch, tmp_path, version
):
    import research_copilot.orchestration.factory as assembly

    await factory.store.mutate_control(
        context(), lambda state: state.role_data.update(workflow_version=version)
    )
    original, builds = assembly.build_supervisor, []

    def build(**kwargs):
        builds.append(kwargs["report_id"])
        return original(**kwargs)

    monkeypatch.setattr(assembly, "build_supervisor", build)
    session = SessionService(
        store=factory.store, agent_factory=factory, policy_version="policy-1"
    )
    profile = cProfile.Profile()
    started, cpu = time.perf_counter(), time.process_time()
    profile.enable()
    try:
        statuses = [await session.get_status("report-1", "actor-1") for _ in range(5)]
    finally:
        profile.disable()
        profile.dump_stats(str(tmp_path / "status.prof"))
        (tmp_path / "timing.json").write_text(
            json.dumps(
                {
                    "wall_seconds": time.perf_counter() - started,
                    "cpu_seconds": time.process_time() - cpu,
                    "builds": len(builds),
                }
            )
        )
    assert all(status == statuses[0] for status in statuses)
    assert len(builds) == 1, "Repeated status reads rebuilt the entire graph"


@pytest.mark.parametrize("version", ["products-v2"])
async def test_cached_reader_observes_new_checkpoints_and_cannot_execute(
    factory, version
):
    await factory.store.mutate_control(
        context(), lambda state: state.role_data.update(workflow_version=version)
    )
    record = await factory.store.load("report-1", "actor-1")
    reader = await factory.for_status(record, RunControl())
    executable = await factory(record, RunControl())
    config = {"configurable": {"thread_id": "v2:report-1"}}
    assert not (await reader.aget_state(config, subgraphs=True)).values
    for value in ("first", "changed"):
        await executable.aupdate_state(
            config,
            {"messages": [HumanMessage(content=value, id="fixture-message")]},
            as_node="__start__",
        )
        assert (await factory.for_status(record, RunControl())) is reader
        assert (await reader.aget_state(config, subgraphs=True)).values["messages"][
            -1
        ].content == value
    for operation in ("ainvoke", "invoke", "astream", "aupdate_state"):
        assert not hasattr(reader, operation)
    with pytest.raises(ValueError, match="own current report"):
        await reader.aget_state({"configurable": {"thread_id": "v2:other"}})
    with pytest.raises(ValueError, match="own current report"):
        await reader.aget_state(
            {
                "configurable": {
                    "thread_id": "v2:report-1",
                    "checkpoint_id": "historical",
                }
            }
        )


async def test_concurrent_reads_coalesce_and_changed_identity_rebuilds(
    factory, monkeypatch
):
    import research_copilot.orchestration.factory as assembly

    await factory.store.mutate_control(
        context(), lambda state: state.role_data.update(workflow_version="products-v2")
    )
    record = await factory.store.load("report-1", "actor-1")
    original, built = assembly.build_supervisor, []

    def build(**kwargs):
        built.append(kwargs["report_id"])
        return original(**kwargs)

    monkeypatch.setattr(assembly, "build_supervisor", build)
    readers = await asyncio.gather(
        *(factory.for_status(record, RunControl()) for _ in range(12))
    )
    assert len(built) == 1 and all(reader is readers[0] for reader in readers)
    await factory.store.save(replace(record, run_id="next-run"))
    next_reader = await factory.for_status(record, RunControl())
    assert len(built) == 2 and next_reader is not readers[0]
    with pytest.raises(LookupError):
        await factory.for_status(replace(record, actor_id="other"), RunControl())
    with pytest.raises(PermissionError):
        await factory.for_status(replace(record, budget_id="other"), RunControl())


async def test_workflow_identity_checked_before_cache_and_legacy_not_cached(factory):
    await factory.store.mutate_control(
        context(), lambda state: state.role_data.update(workflow_version="products-v2")
    )
    record = await factory.store.load("report-1", "actor-1")
    reader = await factory.for_status(record, RunControl())
    await factory.store.mutate_control(
        context(), lambda state: state.role_data.update(workflow_version="unknown")
    )
    with pytest.raises(PermissionError, match="read-only"):
        await factory.for_status(record, RunControl())
    await factory.store.mutate_control(
        context(), lambda state: state.role_data.pop("workflow_version")
    )
    with pytest.raises(PermissionError, match="read-only"):
        await factory.for_status(record, RunControl())
    session = SessionService(
        store=factory.store, agent_factory=factory, policy_version="policy-1"
    )
    assert (await session.get_status("report-1", "actor-1")).status == "read_only"
    assert reader is not None


async def test_failed_build_is_not_cached(factory, monkeypatch):
    import research_copilot.orchestration.factory as assembly

    await factory.store.mutate_control(
        context(), lambda state: state.role_data.update(workflow_version="products-v2")
    )
    record = await factory.store.load("report-1", "actor-1")
    original = assembly.build_supervisor

    def fail(**kwargs):
        raise ValueError("fixture build failure")

    monkeypatch.setattr(assembly, "build_supervisor", fail)
    with pytest.raises(ValueError, match="fixture build failure"):
        await factory.for_status(record, RunControl())
    monkeypatch.setattr(assembly, "build_supervisor", original)
    reader = await factory.for_status(record, RunControl())
    assert await factory.for_status(record, RunControl()) is reader


async def test_deleted_report_cannot_be_read_through_cached_factory(factory):
    await factory.store.mutate_control(
        context(), lambda state: state.role_data.update(workflow_version="products-v2")
    )
    record = await factory.store.load("report-1", "actor-1")
    await factory.for_status(record, RunControl())
    await factory.store.delete_if_idle("report-1", "actor-1")
    with pytest.raises(LookupError):
        await factory.for_status(record, RunControl())


def test_reader_capacity_evicts_lru_and_replaces_old_report_identity():
    from research_copilot.orchestration.status_reader import StatusReaders

    cache = StatusReaders(capacity=2)
    a, b, c = object(), object(), object()
    cache.put(("actor", "a", "run1"), a)
    cache.put(("actor", "b", "run1"), b)
    assert cache.get(("actor", "a", "run1")) is a
    cache.put(("actor", "c", "run1"), c)
    assert cache.get(("actor", "b", "run1")) is None
    cache.put(("actor", "a", "run2"), b)
    assert cache.get(("actor", "a", "run1")) is None
    assert cache.get(("actor", "c", "run1")) is c


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
