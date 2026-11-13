"""Actual PostgreSQL and OS process loss while another report is already active."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_copilot.runtime.control import RunControl
from research_copilot.runtime.maintenance import MaintenanceBusy
from research_copilot.runtime.maintenance import MaintenanceLost
from research_copilot.runtime.runtime_ownership import list_owners, recover_owner
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.sandbox.config import SandboxConfig
from research_copilot.sandbox.receipts import ExecutionService, SandboxQueueTimeout
from research_copilot.sandbox.runner import DockerRunner, ExecutionUnknown
from research_copilot.runtime.sandbox_slots import list_claims
from tests.integration.test_maintenance_postgres import databases

pytestmark = pytest.mark.db
__all__ = ["databases"]


@pytest.mark.parametrize(
    "docker", [False, pytest.param(True, marks=pytest.mark.docker)]
)
async def test_existing_report_cannot_reuse_dead_owners_sandbox_slot(
    databases, tmp_path, docker
):
    url = databases[0]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("RC_")
    }
    environment["RC_TEST_DATABASE_URL"] = url
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    execution_id = "exec-" + uuid4().hex
    runner = DockerRunner()
    name = "rc-analysis-" + execution_id.removeprefix("exec-")

    def sandbox(store):
        return ExecutionService(
            SimpleNamespace(workspace_root=tmp_path),
            control=RunControl(),
            budget=None,
            config=SandboxConfig(concurrency=1, queue_seconds=0.2),
            admission_check=store.check_external_admission,
            slot_claims=store.sandbox_slots,
        )

    with (tmp_path / "owner.log").open("wb") as log:
        async with postgres_runtime(url) as (second, _):
            # This report entered before the other owner was lost. A check only
            # at report-lock acquisition cannot fence its later tool executions.
            async with second.lock("report-2"):
                service = sandbox(second)
                process = subprocess.Popen(
                    [
                        sys._base_executable,
                        "-m",
                        "tests.fakes.sandbox_slot_owner",
                        str(tmp_path),
                        execution_id,
                    ]
                    + (["docker"] if docker else []),
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    stdout=log,
                    stderr=log,
                )
                try:
                    async with asyncio.timeout(30):
                        while not (tmp_path / "ready").exists():
                            assert process.poll() is None, (
                                tmp_path / "owner.log"
                            ).read_text("utf-8", errors="replace")
                            await asyncio.sleep(0.05)
                    owner = next(
                        row
                        for row in await list_owners(url)
                        if row["process_id"] == process.pid
                    )
                    assert owner["connected"]
                    with pytest.raises(SandboxQueueTimeout):
                        async with service._locks("report-2", "dispatch-2"):
                            pytest.fail("Live process slot was reused")
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=10)
                    if docker:
                        code, out, _, _ = await runner._command(
                            "inspect", name, "--format", "{{.State.Running}}"
                        )
                        assert code == 0 and out.strip() == b"true", (
                            "External container must outlive the killed owner"
                        )
                        with pytest.raises(ExecutionUnknown):
                            await runner.confirm_cleanup(execution_id)
                    with pytest.raises(MaintenanceBusy, match="unsettled"):
                        async with service._locks("report-2", "dispatch-2"):
                            pytest.fail(
                                "Dead owner's released slot was reused before recovery"
                            )
                    assert second.runtime_watch.available
                    held = await list_claims(url)
                    assert len(held) == 1 and held[0]["execution_id"] == execution_id
                finally:
                    if process.poll() is None:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=10)
                    if docker:
                        await runner._command("rm", "-f", name)
                        await runner._command("volume", "rm", name + "-outputs")
                        await runner.confirm_cleanup(execution_id)
    owners = await list_owners(url)
    assert len(owners) == 1 and not owners[0]["connected"]
    await recover_owner(
        url,
        owner["id"],
        actor_id="operator",
        note="Controlled owner exited; fixture external resources absent or explicitly removed and verified",
        owner_stopped=True,
    )
    assert await list_claims(url) == []
    async with postgres_runtime(url) as (third, _):
        async with third.lock("report-2"):
            async with sandbox(third)._locks("report-2", "dispatch-2"):
                pass


async def test_unknown_slot_is_fenced_while_its_owner_is_still_connected(databases):
    url = databases[0]
    with pytest.raises(MaintenanceLost, match="ownership"):
        async with postgres_runtime(url) as (first, _):
            async with postgres_runtime(url) as (second, _):
                with pytest.raises(ConnectionError):
                    async with first.sandbox_slots.hold(
                        0, "report-1", "dispatch-1", "exec-" + "a" * 32
                    ) as lease:
                        lease.external_started = True
                        raise ConnectionError(
                            "Unknown Docker outcome with healthy PG owner"
                        )
                assert all(row["connected"] for row in await list_owners(url))
                # No backend-loss detection is involved in the capacity fence.
                with pytest.raises(MaintenanceBusy):
                    async with second.sandbox_slots.hold(
                        0, "report-2", "dispatch-2", "exec-" + "b" * 32
                    ):
                        pytest.fail(
                            "Unknown capacity reused while owner remained connected"
                        )
                async with second.sandbox_slots.hold(
                    1, "report-2", "dispatch-2", "exec-" + "b" * 32
                ):
                    pass
                owner_id = (await list_claims(url))[0]["owner_id"]
    assert len(await list_owners(url)) == 1
    evidence = dict(
        actor_id="operator",
        note="Fake external operation only; both runtimes exited",
        owner_stopped=True,
    )
    receipt = await recover_owner(url, owner_id, **evidence)
    assert await list_claims(url) == []
    assert await recover_owner(url, owner_id, **evidence) == receipt
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        url, autocommit=True
    ) as connection:
        rows = await (
            await connection.execute(
                "SELECT state,recovery_actor,recovery_note FROM sandbox_slot_claims WHERE owner_id=%s",
                (owner_id,),
            )
        ).fetchall()
        assert rows == [("recovered", "operator", evidence["note"])]


async def test_two_connected_owners_cannot_reserve_one_slot_concurrently(databases):
    from research_copilot.runtime.sandbox_slots import SandboxSlotBusy

    finish = asyncio.Event()
    async with postgres_runtime(databases[0]) as (first, _):
        async with postgres_runtime(databases[0]) as (second, _):

            async def reserve(store, digit):
                try:
                    async with store.sandbox_slots.hold(
                        0, "report-" + digit, "dispatch-" + digit, "exec-" + digit * 32
                    ):
                        await finish.wait()
                        return "released"
                except SandboxSlotBusy:
                    return "rejected"

            tasks = [
                asyncio.create_task(reserve(store, digit))
                for store, digit in ((first, "1"), (second, "2"))
            ]
            try:
                done, pending = await asyncio.wait(
                    tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED
                )
                assert len(done) == len(pending) == 1
                assert next(iter(done)).result() == "rejected"
                assert len(await list_claims(databases[0])) == 1
            finally:
                finish.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            assert sorted(results) == ["rejected", "released"]
            assert await list_claims(databases[0]) == []
