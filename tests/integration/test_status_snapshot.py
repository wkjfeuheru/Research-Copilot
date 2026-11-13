from dataclasses import replace
from types import SimpleNamespace

import pytest

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord
from research_copilot.web.app import WorkbenchBackend
from research_copilot.web.status import status_payload
from research_copilot.web.workspace import build_workspace


@pytest.mark.parametrize("projection", ["observability", "workspace"])
async def test_start_transition_after_first_read_keeps_status_snapshot_coherent(
    role_env, tmp_path, monkeypatch, projection
):
    store = role_env[2]
    record = SessionRecord(
        "racing-report", "actor-1", "v2", "policy-1", "snapshot-budget"
    )
    await store.create(
        record, TokenBudget(limit=1000, budget_id=record.budget_id).snapshot()
    )
    advanced = False
    original_load = store.load

    async def advance():
        nonlocal advanced
        if not advanced:
            advanced = True
            await store.save(replace(record, run_id="started-run", status="running"))

    async def load(*args):
        value = await original_load(*args)
        await advance()
        return value

    monkeypatch.setattr(store, "load", load)
    if hasattr(store, "read_snapshot"):
        original_snapshot = store.read_snapshot

        async def snapshot(*args):
            value = await original_snapshot(*args)
            await advance()
            return value

        monkeypatch.setattr(store, "read_snapshot", snapshot)
    if projection == "observability":
        result = await status_payload(store, "actor-1", record.report_id)
        assert result["session_status"] == "created"
    else:
        backend = WorkbenchBackend(
            session=SimpleNamespace(store=store),
            store=store,
            actor_id="actor-1",
            workspace_root=tmp_path,
        )
        result = await build_workspace(backend, record.report_id)
        assert result["report"]["status"] == "created"
    assert advanced
    assert (await original_load(record.report_id, "actor-1")).run_id == "started-run"


async def test_old_source_contract_is_presented_read_only(role_env, tmp_path):
    store = role_env[2]

    def seed(state):
        state.role_data["workflow_version"] = "products-v2"
        state.role_data["runtime_manifest"] = {
            "source_contract": {"sha256": "c" * 64, "normalization_version": 1}
        }

    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.runtime.session import SessionRecord

    record = SessionRecord(
        "old-contract", "actor-1", "v2", "v2-policy-1", "old-contract"
    )
    await store.create(
        record, TokenBudget(limit=1000, budget_id=record.budget_id).snapshot()
    )
    from research_copilot.contracts import RunContext

    await store.mutate_control(
        RunContext("actor-1", record.report_id, "", record.budget_id, "v2-policy-1"),
        seed,
    )
    backend = WorkbenchBackend(
        session=SimpleNamespace(
            store=store,
            agent_factory=SimpleNamespace(
                source_contract={"sha256": "d" * 64, "normalization_version": 2}
            ),
        ),
        store=store,
        actor_id="actor-1",
        workspace_root=tmp_path,
    )
    result = await build_workspace(backend, record.report_id)
    assert result["read_only"] is True
    assert result["report"]["status"] == "read_only"


async def test_snapshot_still_rejects_wrong_actor(role_env):
    with pytest.raises(LookupError):
        await role_env[2].read_snapshot("report-1", "another-actor")
