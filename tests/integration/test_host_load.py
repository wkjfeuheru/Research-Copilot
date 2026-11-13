"""Measured real PG/HTTP load; synthetic history/model, no paid services or Docker."""

import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from research_copilot import host
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.runtime_ownership import list_owners
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import sessions
from tests.integration.test_maintenance_postgres import databases

__all__ = ["databases"]
pytestmark = [pytest.mark.db, pytest.mark.load]


def summary(values):
    ordered = sorted(values)
    return {
        key: ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]
        for key, fraction in (
            ("p50_ms", 0.5),
            ("p95_ms", 0.95),
            ("p99_ms", 0.99),
            ("max_ms", 1),
        )
    }


async def test_host_load(databases, tmp_path):
    url, workspace = databases[0], tmp_path / "workspace"
    controls = tmp_path / "controls"
    (controls / "calls").mkdir(parents=True)
    (controls / "measure").touch()
    environment = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("RC_")
    }
    environment.update(RC_TEST_DATABASE_URL=url, PYTHONPATH=os.pathsep.join(sys.path))
    processes, logs, urls, streams = [], [], [], []
    results = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "history": {"total": 2000, "actor_rows": 250, "control_payload_bytes": 16384},
        "synthetic_model": True,
        "phases": [],
        "database_samples": [],
    }

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
        async with asyncio.timeout(45):
            while True:
                assert process.poll() is None, (controls / (label + ".log")).read_text(
                    errors="replace"
                )
                try:
                    ready = json.loads((controls / (label + ".ready")).read_text())
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    await asyncio.sleep(0.05)
        assert ready["pid"] == process.pid
        urls.append(f"http://127.0.0.1:{ready['port']}")

    async def poll_until(check):
        async with asyncio.timeout(60):
            while not await check():
                await asyncio.sleep(0.1)

    async def hold_stream(client, base, report_id, opened):
        async with client.stream(
            "GET", base + f"/api/reports/{report_id}/events"
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("event: workspace"):
                    opened.set()

    stop_sampling = asyncio.Event()

    async def database_samples(connection):
        while not stop_sampling.is_set():
            row = await (
                await connection.execute("""
                SELECT count(*) AS connections,
                    count(*) FILTER (WHERE state='active') AS active,
                    count(*) FILTER (WHERE wait_event_type='Lock') AS lock_waits
                FROM pg_stat_activity WHERE datname=current_database() AND pid <> pg_backend_pid()
            """)
            ).fetchone()
            jobs = await (
                await connection.execute(
                    "SELECT status,count(*) AS count FROM research_run_jobs GROUP BY status"
                )
            ).fetchall()
            results["database_samples"].append(
                {
                    "time": time.time(),
                    **row,
                    "jobs": {item["status"]: item["count"] for item in jobs},
                }
            )
            await asyncio.sleep(0.25)

    async def phase(client, label, readers, count, report_id):
        semaphore = asyncio.Semaphore(readers)
        samples = []
        started = time.time()

        async def request(index):
            async with semaphore:
                path = "/api/reports" if index % 2 == 0 else f"/api/reports/{report_id}"
                before = time.perf_counter()
                try:
                    response = await client.get(urls[index % len(urls)] + path)
                    status = response.status_code
                    if status == 200 and path == "/api/reports":
                        assert len(response.json()["reports"]) == 255
                    size = len(response.content)
                except httpx.HTTPError as exc:
                    status, size = type(exc).__name__, 0
                samples.append(
                    {
                        "route": "list" if index % 2 == 0 else "workspace",
                        "ms": (time.perf_counter() - before) * 1000,
                        "status": status,
                        "bytes": size,
                    }
                )

        await asyncio.gather(*(request(index) for index in range(count)))
        elapsed = time.time() - started
        results["phases"].append(
            {
                "label": label,
                "hosts": len(urls),
                "readers": readers,
                "count": count,
                "started": started,
                "ended": time.time(),
                "seconds": elapsed,
                "requests_per_second": count / elapsed,
                "status": dict(Counter(str(s["status"]) for s in samples)),
                "latency": {
                    route: summary([s["ms"] for s in samples if s["route"] == route])
                    for route in ("list", "workspace")
                },
                "requests": samples,
            }
        )

    sampling = None
    try:
        async with await psycopg.AsyncConnection.connect(
            url, autocommit=True, row_factory=dict_row
        ) as connection:
            # Synthetic historical storage only. No checkpoint or signed report
            # is fabricated; these rows exercise only the list summary query.
            await connection.execute("""
                INSERT INTO sessions (report_id,actor_id,runtime_version,policy_version,budget_id,run_id,status,cancel_requested,user_request,created_at,control,budget)
                SELECT 'history-'||i, CASE WHEN i<=250 THEN 'actor-1' ELSE 'other' END,
                    'v2','policy-1','history-'||i,'history-'||i,'created',false,'Synthetic load history',
                    '2026-01-01T00:00:00Z',json_build_object('role_data',json_build_object('load_payload',p.payload)),'{}'
                FROM generate_series(1,2000) i CROSS JOIN
                    (SELECT string_agg(md5(j::text),'') AS payload FROM generate_series(1,512) j) p
            """)
            await connection.execute("ANALYZE sessions")
            results["postgres"] = (
                await (
                    await connection.execute("SELECT version() AS version")
                ).fetchone()
            )["version"]
            results["indexes"] = await (
                await connection.execute(
                    "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename IN ('sessions','research_run_jobs')"
                )
            ).fetchall()
            # Compare full-row and metadata-only server execution. EXPLAIN does
            # not include driver JSON decoding/network costs; HTTP measures those.
            statements = {
                "full_row": "SELECT * FROM sessions WHERE actor_id='actor-1' ORDER BY created_at DESC,report_id DESC",
                "summary_only": "SELECT "
                + ",".join(
                    sessions.c[key].name for key in SessionRecord.__dataclass_fields__
                )
                + " FROM sessions WHERE actor_id='actor-1' ORDER BY created_at DESC,report_id DESC",
            }
            results["query_plans"] = {}
            for label, statement in statements.items():
                rows = await (
                    await connection.execute(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement
                    )
                ).fetchone()
                results["query_plans"][label] = rows["QUERY PLAN"]
            await host.bind_host_configuration(
                HostSettings(_env_file=None, database_url=url),
                workspace_root=workspace,
                models_path=Path("config/models.example.toml"),
                policy_version="policy-1",
                mode="test",
                actor_id="operator",
                note="Isolated measured load fixture",
            )
            await launch("first")
            (controls / "multi-host-case-1.release").touch()
            async with httpx.AsyncClient(
                timeout=30,
                trust_env=False,
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            ) as client:
                report_ids = []
                for index in range(1, 6):
                    response = await client.post(
                        urls[0] + "/api/reports",
                        json={"user_request": f"multi-host-case-{index}"},
                    )
                    assert response.status_code == 201, response.text
                    report_ids.append(response.json()["report"]["id"])
                    if index == 1:

                        async def paused():
                            return bool(
                                (
                                    await client.get(
                                        urls[0] + f"/api/reports/{report_ids[0]}"
                                    )
                                ).json()["interrupts"]
                            )

                        await poll_until(paused)

                async def running():
                    return (
                        await (
                            await connection.execute(
                                "SELECT count(*) AS n FROM research_run_jobs WHERE status='running'"
                            )
                        ).fetchone()
                    )["n"] == 3

                await poll_until(running)
                sampling = asyncio.create_task(database_samples(connection))
                await phase(client, "single-serial", 1, 20, report_ids[0])
                await phase(client, "single-concurrent", 8, 80, report_ids[0])
                await launch("second")
                for index in range(12):
                    opened = asyncio.Event()
                    streams.append(
                        asyncio.create_task(
                            hold_stream(client, urls[index % 2], report_ids[0], opened)
                        )
                    )
                    await asyncio.wait_for(opened.wait(), 30)
                await phase(client, "dual-sse-concurrent", 24, 120, report_ids[0])
                for task in streams:
                    assert not task.done(), (
                        task.exception() if not task.cancelled() else "Cancelled"
                    )
                    task.cancel()
                await asyncio.gather(*streams, return_exceptions=True)
                for index in range(2, 6):
                    (controls / f"multi-host-case-{index}.release").touch()
                stop_sampling.set()
                await sampling

                async def settled():
                    return (
                        await (
                            await connection.execute(
                                "SELECT count(*) AS n FROM research_run_jobs WHERE status NOT IN ('succeeded','failed','cancelled')"
                            )
                        ).fetchone()
                    )["n"] == 0

                await poll_until(settled)
                results["final_jobs"] = await (
                    await connection.execute(
                        "SELECT status,count(*) AS count FROM research_run_jobs GROUP BY status"
                    )
                ).fetchall()
        for label in ("first", "second"):
            (controls / (label + ".stop")).touch()
        for process in processes:
            await asyncio.to_thread(process.wait, timeout=30)
            assert process.returncode == 0
        assert await list_owners(url) == []
        calls = [
            json.loads(path.read_text()) for path in (controls / "calls").glob("*.json")
        ]
        assert len(calls) == len({item["request"] for item in calls}) == 5
        results["host_samples"] = {
            label: [
                json.loads(line)
                for line in (controls / (label + ".metrics.jsonl"))
                .read_text()
                .splitlines()
            ]
            for label in ("first", "second")
        }
        assert all(
            phase["status"] == {"200": phase["count"]} for phase in results["phases"]
        )
        assert results["final_jobs"] == [{"status": "succeeded", "count": 5}]
        assert all(
            sum(
                sample["jobs"].get(s, 0)
                for s in ("running", "claimed", "recovery_required")
            )
            <= 3
            for sample in results["database_samples"]
        )
    finally:
        stop_sampling.set()
        if sampling is not None:
            await asyncio.gather(sampling, return_exceptions=True)
        for task in streams:
            task.cancel()
        await asyncio.gather(*streams, return_exceptions=True)
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
        (tmp_path / "measurements.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )
