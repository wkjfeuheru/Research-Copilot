"""Two real Web processes, native shared workspace, real PG; synthetic planners."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from filelock import Timeout
import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from research_copilot import host
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.runtime_ownership import list_owners
from research_copilot.runtime.workspace_lock import workspace_lease
from tests.integration.test_maintenance_postgres import databases

__all__ = ["databases"]
pytestmark = pytest.mark.db


async def test_two_web_processes_share_capacity_checkpoint_and_shutdown(
    databases, tmp_path
):
    url, workspace = databases[0], tmp_path / "workspace"
    controls = tmp_path / "controls"
    (controls / "calls").mkdir(parents=True)
    settings = HostSettings(_env_file=None, database_url=url)
    await host.bind_host_configuration(
        settings,
        workspace_root=workspace,
        models_path=Path("config/models.example.toml"),
        policy_version="policy-1",
        mode="test",
        actor_id="operator",
        note="Isolated shared-host fixture",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("RC_")
    }
    environment.update(RC_TEST_DATABASE_URL=url, PYTHONPATH=os.pathsep.join(sys.path))
    processes, logs = [], []

    async def wait_file(path, timeout=30):
        async with asyncio.timeout(timeout):
            while not path.exists():
                await asyncio.sleep(0.02)

    async def launch(label):
        log = (controls / (label + ".log")).open("wb")
        logs.append(log)
        process = subprocess.Popen(
            [
                sys._base_executable,
                "-m",
                "tests.fakes.shared_web_host",
                str(controls),
                str(workspace),
                label,
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdout=log,
            stderr=log,
        )
        processes.append(process)
        async with asyncio.timeout(30):
            while True:
                assert process.poll() is None, (controls / (label + ".log")).read_text(
                    "utf-8", errors="replace"
                )
                try:
                    ready = json.loads(
                        (controls / (label + ".ready")).read_text("utf-8")
                    )
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    await asyncio.sleep(0.05)
        assert ready["pid"] == process.pid
        return process, f"http://127.0.0.1:{ready['port']}"

    def calls():
        result = []
        for path in (controls / "calls").glob("*.json"):
            try:
                result.append(json.loads(path.read_text("utf-8")))
            except json.JSONDecodeError:
                pass
        return result

    async def wait_calls(count):
        async with asyncio.timeout(30):
            while len(calls()) < count:
                await asyncio.sleep(0.05)

    async def jobs(connection):
        return await (
            await connection.execute(
                "SELECT report_id,status,claim_token,worker_id FROM research_run_jobs ORDER BY created_at"
            )
        ).fetchall()

    async def wait_paused(client, base, report_id):
        async with asyncio.timeout(30):
            while True:
                response = await client.get(base + "/api/reports/" + report_id)
                assert response.status_code == 200, response.text
                value = response.json()
                if value["interrupts"]:
                    assert value["interrupts"][0]["tool"] == "confirm_plan"
                    return value
                await asyncio.sleep(0.05)

    try:
        (controls / "multi-host-case-1.release").touch()
        first, first_url = await launch("first")
        async with httpx.AsyncClient(timeout=15) as client:
            async with await psycopg.AsyncConnection.connect(
                url, autocommit=True, row_factory=dict_row
            ) as connection:
                report_ids = []
                for index in range(1, 5):
                    response = await client.post(
                        first_url + "/api/reports",
                        json={"user_request": f"multi-host-case-{index}"},
                    )
                    assert response.status_code == 201, response.text
                    report_ids.append(response.json()["report"]["id"])
                    if index == 1:
                        paused = await wait_paused(client, first_url, report_ids[0])
                await wait_calls(4)
                before = await jobs(connection)
                active = {
                    row["report_id"]: row
                    for row in before
                    if row["status"] == "running"
                }
                assert len(active) == 3
                second, second_url = await launch("second")
                owners = await list_owners(url)
                assert {row["process_id"] for row in owners} == {first.pid, second.pid}
                assert all(row["connected"] for row in owners)
                second_view = await wait_paused(client, second_url, report_ids[0])
                assert (
                    second_view["interrupts"][0]["id"] == paused["interrupts"][0]["id"]
                )
                response = await client.post(
                    second_url + "/api/reports",
                    json={"user_request": "multi-host-case-5"},
                )
                assert response.status_code == 201, response.text
                report_ids.append(response.json()["report"]["id"])
                for _ in range(20):
                    rows = await jobs(connection)
                    assert {
                        row["report_id"]: row
                        for row in rows
                        if row["report_id"] in active
                    } == active
                    assert rows[-1]["status"] == "pending"
                    assert (
                        sum(
                            row["status"] in {"claimed", "running", "recovery_required"}
                            for row in rows
                        )
                        == 3
                    )
                    assert len(calls()) == 4
                    await asyncio.sleep(0.05)
                with pytest.raises(Timeout):
                    with workspace_lease(workspace):
                        pytest.fail("Maintenance overlapped two Web hosts")
                (controls / "first.stop").touch()
                await wait_file(controls / "first.draining")
                for index in range(2, 5):
                    (controls / f"multi-host-case-{index}.release").touch()
                await asyncio.to_thread(first.wait, timeout=30)
                assert first.returncode == 0 and (controls / "first.closed").exists()
                await wait_calls(5)
                assert (
                    next(
                        row for row in calls() if row["request"] == "multi-host-case-5"
                    )["pid"]
                    == second.pid
                )
                owners = await list_owners(url)
                assert len(owners) == 1 and owners[0]["process_id"] == second.pid
                with pytest.raises(Timeout):
                    with workspace_lease(workspace):
                        pytest.fail(
                            "First host shutdown released the second host's lock"
                        )
                (controls / "multi-host-case-5.release").touch()
                await wait_paused(client, second_url, report_ids[-1])
                final = await jobs(connection)
                assert all(
                    row["status"] in {"succeeded", "failed", "cancelled"}
                    for row in final
                )
                assert {
                    row["report_id"]: row["claim_token"]
                    for row in final
                    if row["report_id"] in active
                } == {key: row["claim_token"] for key, row in active.items()}
                assert (
                    len(calls()) == 5 and len({row["request"] for row in calls()}) == 5
                )
                assert {row["pid"] for row in calls()} == {first.pid, second.pid}
        (controls / "second.stop").touch()
        await asyncio.to_thread(second.wait, timeout=30)
        assert second.returncode == 0 and (controls / "second.closed").exists()
        assert await list_owners(url) == []
        with workspace_lease(workspace):
            pass
    finally:
        for index in range(1, 6):
            (controls / f"multi-host-case-{index}.release").touch()
        for label in ("first", "second"):
            (controls / (label + ".stop")).touch()
        for process in processes:
            if process.poll() is None:
                try:
                    await asyncio.to_thread(process.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=10)
        for log in logs:
            log.close()
