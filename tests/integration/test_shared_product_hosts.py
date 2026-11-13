"""Two OS Web hosts and real Docker; synthetic provider responses only."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from research_copilot import host
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.identity import SESSION_TABLE_NAME
from research_copilot.runtime.runtime_ownership import list_owners
from research_copilot.sandbox.runner import DockerRunner
from tests.integration.test_maintenance_postgres import databases

__all__ = ["databases"]
pytestmark = [pytest.mark.db, pytest.mark.docker]


async def test_two_web_hosts_share_real_sandbox_capacity_and_resume_products(
    databases, tmp_path
):
    url, workspace = databases[0], tmp_path / "workspace"
    controls = tmp_path / "controls"
    controls.mkdir()
    await host.bind_host_configuration(
        HostSettings(_env_file=None, database_url=url),
        workspace_root=workspace,
        models_path=Path("config/models.example.toml"),
        policy_version="policy-1",
        mode="test",
        actor_id="operator",
        note="Isolated parallel product fixture",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("RC_")
    }
    environment.update(RC_TEST_DATABASE_URL=url, PYTHONPATH=os.pathsep.join(sys.path))
    processes, logs, names, samples = {}, [], set(), []
    runner = DockerRunner()

    async def launch(label):
        log = (controls / (label + ".log")).open("wb")
        logs.append(log)
        process = subprocess.Popen(
            [
                sys._base_executable,
                "-m",
                "tests.fakes.shared_product_host",
                str(controls),
                str(workspace),
                label,
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdout=log,
            stderr=log,
        )
        processes[label] = process
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
        return f"http://127.0.0.1:{ready['port']}"

    async def stop(label):
        (controls / (label + ".stop")).touch()
        await asyncio.to_thread(processes[label].wait, timeout=30)
        assert (
            processes[label].returncode == 0
            and (controls / (label + ".closed")).exists()
        )

    async def docker(*arguments):
        code, out, err, over = await runner._command(*arguments)
        assert code == 0 and not over, (arguments, out, err)
        return out

    async def claims(connection):
        rows = await (
            await connection.execute(
                "SELECT c.*,o.process_id FROM sandbox_slot_claims c JOIN runtime_owners o ON c.owner_id=o.id ORDER BY c.started_at"
            )
        ).fetchall()
        for row in rows:
            names.add("rc-analysis-" + row["execution_id"].removeprefix("exec-"))
        return rows

    async def running(connection, count):
        async with asyncio.timeout(45):
            while True:
                rows, physical = await capacity(connection)
                live = [
                    row
                    for row in rows
                    if "rc-analysis-" + row["execution_id"].removeprefix("exec-")
                    in physical
                ]
                if len(live) == count:
                    return live
                await asyncio.sleep(0.1)

    async def capacity(connection):
        # Include containers for released rows: held claims alone could hide
        # a premature release while an older container is still running.
        rows = [row for row in await claims(connection) if row["state"] == "held"]
        raw = await docker(
            "ps",
            "--filter",
            "label=research-copilot.analysist-sandbox=1",
            "--format",
            "{{.Names}}",
        )
        physical = set(raw.decode().splitlines()) & names
        samples.append(
            {
                "held": len(rows),
                "physical": sorted(physical),
                "owner_pids": sorted({row["process_id"] for row in rows}),
            }
        )
        assert len(rows) <= 2 and len(physical) <= 2, samples[-1]
        return rows, physical

    async def release(row):
        name = "rc-analysis-" + row["execution_id"].removeprefix("exec-")
        await docker("exec", name, "touch", "/tmp/fixture-release")

    try:
        first = await launch("first")
        async with (
            httpx.AsyncClient(timeout=20) as client,
            await psycopg.AsyncConnection.connect(
                url, autocommit=True, row_factory=dict_row
            ) as connection,
        ):

            async def view(base, report):
                result = await client.get(base + "/api/reports/" + report)
                assert result.status_code == 200, result.text
                value = result.json()
                assert not value["runtime"]["last_error"], value
                return value

            async def gate(base, report, name):
                async with asyncio.timeout(60):
                    while True:
                        value = await view(base, report)
                        if value["interrupts"]:
                            current = value["interrupts"][0]
                            assert (current["tool"] or current["kind"]) == name, value
                            return current
                        await asyncio.sleep(0.1)

            async def decide(base, report, current, decision=None):
                result = await client.post(
                    base + "/api/reports/" + report + "/decisions",
                    json={
                        "decisions": {
                            current["id"]: decision
                            or {"decisions": [{"type": "approve"}]}
                        }
                    },
                )
                assert result.status_code == 200, result.text

            async def create(base):
                result = await client.post(
                    base + "/api/reports",
                    json={
                        "user_request": "Isolated parallel product fixture; no live model or source"
                    },
                )
                assert result.status_code == 201, result.text
                report = result.json()["report"]["id"]
                await decide(base, report, await gate(base, report, "confirm_plan"))
                return report

            reports = [await create(first), await create(first)]
            occupied = await running(connection, 2)
            assert {row["process_id"] for row in occupied} == {processes["first"].pid}
            second = await launch("second")
            reports.append(await create(second))
            # Wait for the third real model to issue execute, not merely for an
            # enqueued command. It has no receipt or capacity claim while full.
            async with asyncio.timeout(30):
                while True:
                    path = controls / "providers" / (reports[-1] + ".json")
                    state = json.loads(path.read_text("utf-8")) if path.exists() else {}
                    if state.get("positions", {}).get("analysist", 0) == 2:
                        break
                    await asyncio.sleep(0.05)
            for _ in range(10):
                rows = await claims(connection)
                assert len(rows) == 2 and all(row["state"] == "held" for row in rows)
                assert reports[-1] not in {row["report_id"] for row in rows}
                _, physical = await capacity(connection)
                assert len(physical) == 2
                await asyncio.sleep(0.1)
            await release(occupied[0])
            async with asyncio.timeout(45):
                while True:
                    concurrent = await running(connection, 2)
                    if reports[-1] in {row["report_id"] for row in concurrent}:
                        break
                    await asyncio.sleep(0.1)
            assert {row["process_id"] for row in concurrent} == {
                processes["first"].pid,
                processes["second"].pid,
            }
            for row in concurrent:
                await release(row)
            for report in reports:
                await gate(second, report, "confirm_analysis")
            await stop("first")
            assert len(await list_owners(url)) == 1
            # The second process resumes the first process's persisted graph.
            for report in reports:
                await decide(
                    second, report, await gate(second, report, "confirm_analysis")
                )
                current = await gate(second, report, "reviewer_deep_review")
                review = current["review"]
                assert review["checks_complete"] and review["issues"]
                assert not any(issue["hard"] for issue in review["issues"]), review
                await decide(
                    second,
                    report,
                    current,
                    {
                        "decision": "approve",
                        "comment": "Synthetic fixture scope verified",
                        "snapshot_id": review["snapshot_id"],
                        "review_session_id": review["review_session_id"],
                        "issue_dispositions": [
                            {
                                "issue_id": issue["issue_id"],
                                "decision": "false_positive",
                                "reason": "Verified synthetic fixture numeric lineage",
                            }
                            for issue in review["issues"]
                        ],
                    },
                )
                await decide(
                    second, report, await gate(second, report, "signoff_report")
                )
                async with asyncio.timeout(30):
                    while not (await view(second, report))["signed"]:
                        await asyncio.sleep(0.1)
            history = await claims(connection)
            assert len(history) == 3 and all(
                row["state"] == "released" for row in history
            )
            assert {row["report_id"] for row in history} == set(reports)
            assert len({row["execution_id"] for row in history}) == 3
            for row in history:
                await runner.confirm_cleanup(row["execution_id"])
            await stop("second")
            assert await list_owners(url) == []
            restarted = await launch("restarted")
            chart_ids = []
            for report in reports:
                assert (await view(restarted, report))["signed"]
                record = await (
                    await connection.execute(
                        f"SELECT control FROM {SESSION_TABLE_NAME} WHERE report_id=%s",
                        (report,),
                    )
                ).fetchone()
                state = record["control"]
                assert len(state["role_data"]["executions"]) == 1
                assert len(state["results"]) == 4
                provider = json.loads(
                    (controls / "providers" / (report + ".json")).read_text("utf-8")
                )
                assert (
                    provider["positions"]["analysist"] == 5
                    and provider["image_reads"] > 0
                )
                chart = provider["execution"]["chart_ids"][0]
                chart_ids.append(chart)
                version = state["artifacts"]["report"]["version"]
                response = await client.get(
                    f"{restarted}/api/reports/{report}/versions/{version}/charts/{chart}/png"
                )
                assert response.status_code == 200 and response.content.startswith(
                    b"\x89PNG\r\n\x1a\n"
                )
            assert len(set(chart_ids)) == 3
            wrong = await client.get(
                f"{restarted}/api/reports/{reports[1]}/versions/1/charts/{chart_ids[0]}/png"
            )
            assert wrong.status_code == 404
            assert await claims(connection) == history
            (controls / "acceptance.json").write_text(
                json.dumps(
                    {
                        "reports": reports,
                        "hosts": {
                            label: process.pid for label, process in processes.items()
                        },
                        "capacity_samples": samples,
                        "claims": history,
                        "chart_ids": chart_ids,
                        "signed_after_restart": True,
                        "cross_report_chart_status": wrong.status_code,
                        "model_and_sources": "synthetic fixtures",
                        "post_delivery_extraction": "disabled in this fixture",
                    },
                    default=str,
                    indent=2,
                ),
                encoding="utf-8",
            )
            await stop("restarted")
            assert await list_owners(url) == []
    finally:
        for label in processes:
            (controls / (label + ".stop")).touch()
        for process in processes.values():
            if process.poll() is None:
                try:
                    await asyncio.to_thread(process.wait, timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=10)
        try:
            # Discover late claims after all fixture processes have exited;
            # cleanup is restricted to this disposable database's execution IDs.
            async with await psycopg.AsyncConnection.connect(
                url, autocommit=True, row_factory=dict_row
            ) as connection:
                await claims(connection)
            for name in names:
                await runner._command("rm", "-f", name)
                await runner._command("volume", "rm", name + "-outputs")
                await runner.confirm_cleanup(
                    "exec-" + name.removeprefix("rc-analysis-")
                )
        finally:
            for log in logs:
                log.close()
