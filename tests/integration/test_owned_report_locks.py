"""Report serialization must survive loss of its PostgreSQL lock connection."""

from contextlib import suppress
import asyncio

import psycopg
import pytest

from research_copilot.runtime.identity import advisory_lock_identity
from research_copilot.runtime.maintenance import MaintenanceBusy
from research_copilot.runtime.runtime_ownership import list_owners
from research_copilot.runtime.storage import SessionBusy, postgres_runtime
from tests.integration.test_maintenance_postgres import databases

pytestmark = pytest.mark.db
__all__ = ["databases"]


async def lock_pid(admin, report_id):
    key = advisory_lock_identity(report_id) & ((1 << 64) - 1)
    cursor = await admin.execute(
        "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted "
        "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "AND classid=%s AND objid=%s AND objsubid=1",
        (key >> 32, key & ((1 << 32) - 1)),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    return rows[0][0]


async def test_report_lock_is_owned_by_the_registered_host_connection(databases):
    url = databases[0]
    async with postgres_runtime(url) as (store, _):
        owner = (await list_owners(url))[0]
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
            async with store.lock("report-1"):
                assert await lock_pid(admin, "report-1") == owner["backend_pid"]
                with pytest.raises(SessionBusy):
                    async with store.lock("report-1"):
                        pytest.fail(
                            "Session advisory locks are reentrant; local guard is required"
                        )
                async with store.lock("report-2"):
                    assert await lock_pid(admin, "report-2") == owner["backend_pid"]


async def test_an_existing_host_cannot_overlap_a_lost_report_owner(databases):
    url = databases[0]
    overlapped = False
    denied = False
    async with postgres_runtime(url) as (second, _):
        # Old work deliberately remains inside its scope after lock loss.
        with suppress(Exception):
            async with postgres_runtime(url) as (first, _):
                async with first.lock("report-1"):
                    async with await psycopg.AsyncConnection.connect(
                        url, autocommit=True
                    ) as admin:
                        pid = await lock_pid(admin, "report-1")
                        await admin.execute(
                            "SELECT pg_terminate_backend(%s,5000)", (pid,)
                        )
                    try:
                        async with second.lock("report-1"):
                            overlapped = True
                    except MaintenanceBusy:
                        denied = True
        assert not overlapped, (
            "Existing second host entered while old report work remained live"
        )
        assert denied, (
            "Second host must reject specifically because the old owner is unsettled"
        )


async def test_host_close_keeps_ownership_until_report_scope_drains(databases):
    url = databases[0]
    scope = postgres_runtime(url)
    store, _ = await scope.__aenter__()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def operation():
        async with store.lock("report-1"):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    closing = asyncio.create_task(scope.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0.05)
        assert not closing.done()
        owners = await list_owners(url)
        assert len(owners) == 1 and owners[0]["connected"]
        with pytest.raises(MaintenanceBusy):
            async with postgres_runtime(url, initialize=True):
                pytest.fail("Maintenance entered before report scope drained")
    finally:
        finish.set()
        await task
        await closing
    assert await list_owners(url) == []


async def test_existing_process_is_fenced_after_report_owner_is_killed(
    databases, tmp_path
):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from research_copilot.runtime.runtime_ownership import recover_owner

    url = databases[0]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("RC_")
    }
    environment["RC_TEST_DATABASE_URL"] = url
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    with (tmp_path / "owner.log").open("wb") as log:
        async with postgres_runtime(url) as (second, _):
            process = subprocess.Popen(
                [
                    sys._base_executable,
                    "-m",
                    "tests.fakes.report_lock_owner",
                    str(tmp_path),
                ],
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
                assert owner["connected"] is True
                with pytest.raises(SessionBusy):
                    async with second.lock("report-1"):
                        pytest.fail("Live process overlapped")
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)
                with pytest.raises(MaintenanceBusy, match="Unsettled runtime"):
                    async with second.lock("report-1"):
                        pytest.fail("Unsettled process was ignored")
            finally:
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=10)
    assert not (tmp_path / "closed").exists()
    assert (await list_owners(url))[0]["connected"] is False
    await recover_owner(
        url,
        owner["id"],
        actor_id="operator",
        note="Tracked report owner process exited; fixture starts no external work",
        owner_stopped=True,
    )
    async with postgres_runtime(url) as (third, _):
        async with third.lock("report-1"):
            pass
