"""Persistent physical capacity is independent of OS/connection liveness."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.runtime.runtime_ownership import (
    metadata as owner_metadata,
    owners,
)


@pytest.fixture
async def slot_managers(tmp_path):
    from research_copilot.runtime.sandbox_slots import SandboxSlots, metadata

    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "slots.db"))
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        await connection.run_sync(owner_metadata.create_all)
        await connection.run_sync(metadata.create_all)
        for identity in ("a" * 32, "b" * 32):
            await connection.execute(
                insert(owners).values(
                    id=identity,
                    hostname="fixture",
                    process_id=1,
                    backend_pid=1,
                    backend_started_at=now,
                    started_at=now,
                    state="open",
                )
            )
    try:
        yield SandboxSlots(engine, "a" * 32), SandboxSlots(engine, "b" * 32)
    finally:
        await engine.dispose()


async def test_unknown_execution_retains_capacity_even_with_connected_owner(
    slot_managers,
):
    from research_copilot.runtime.sandbox_slots import SandboxSlotBusy, claims

    first, second = slot_managers
    with pytest.raises(ConnectionError):
        async with first.hold(0, "report-1", "dispatch-1", "exec-" + "1" * 32) as lease:
            lease.external_started = True
            raise ConnectionError("Cannot establish whether container stopped")
    with pytest.raises(SandboxSlotBusy):
        async with second.hold(0, "report-2", "dispatch-2", "exec-" + "2" * 32):
            pytest.fail("Unknown physical execution capacity was reused")
    async with second.hold(1, "report-2", "dispatch-2", "exec-" + "2" * 32):
        pass
    async with first.engine.connect() as connection:
        rows = (
            (await connection.execute(select(claims).order_by(claims.c.slot)))
            .mappings()
            .all()
        )
    assert [row["state"] for row in rows] == ["held", "released"]
    assert rows[0]["owner_id"] == "a" * 32


async def test_confirmed_cleanup_releases_capacity_and_keeps_history(slot_managers):
    from research_copilot.runtime.sandbox_slots import claims

    first, second = slot_managers
    async with first.hold(0, "report-1", "dispatch-1", "exec-" + "1" * 32) as lease:
        lease.external_started = True
        lease.external_stopped = True
    async with second.hold(0, "report-2", "dispatch-2", "exec-" + "2" * 32):
        pass
    async with first.engine.connect() as connection:
        rows = (await connection.execute(select(claims))).mappings().all()
    assert len(rows) == 2 and all(row["state"] == "released" for row in rows)
    assert len({row["id"] for row in rows}) == 2


async def test_physical_admission_skips_quarantined_capacity(slot_managers, tmp_path):
    from types import SimpleNamespace
    from research_copilot.runtime.control import RunControl
    from research_copilot.runtime.maintenance import MaintenanceLost
    from research_copilot.sandbox.config import SandboxConfig
    from research_copilot.sandbox.receipts import ExecutionService

    first, second = slot_managers
    with pytest.raises(MaintenanceLost):
        async with first.hold(0, "report-1", "dispatch-1", "exec-" + "1" * 32) as lease:
            lease.external_started = True
    service = ExecutionService(
        SimpleNamespace(workspace_root=tmp_path),
        control=RunControl(),
        budget=None,
        config=SandboxConfig(concurrency=2),
        slot_claims=second,
    )
    async with service._locks("report-2", "dispatch-2") as slot:
        assert slot == 1
