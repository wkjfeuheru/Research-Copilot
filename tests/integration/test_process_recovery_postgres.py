"""Real OS kill/restart + PostgreSQL checkpoints; no vendor/network model calls."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import URL

from research_copilot.contracts import ArtifactRef, RunContext
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.business import approval_key
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.urls import to_checkpoint_url

pytestmark = pytest.mark.db


def kill_child(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=True,
        )
    else:
        process.kill()
    process.wait(timeout=15)


@pytest.mark.parametrize("window", ["sent", "settled", "business"])
async def test_process_kill_retains_budget_receipts_and_blocks_implicit_resend(
    tmp_path, window
):
    base = os.environ.get("RC_TEST_DATABASE_URL")
    if not base:
        pytest.skip("Requires explicit isolated RC_TEST_DATABASE_URL")
    base = to_checkpoint_url(base)
    params = psycopg.conninfo.conninfo_to_dict(base)
    name = "rc_crash_" + uuid4().hex
    url = URL.create(
        "postgresql",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", 5432)),
        database=name,
    ).render_as_string(hide_password=False)
    context = RunContext("actor-1", "report-1", "run-1", "budget-1", "policy-1")
    process = None
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            async with postgres_runtime(url, initialize=True) as (store, saver):
                await store.create(
                    SessionRecord(
                        "report-1",
                        "actor-1",
                        "v2",
                        "policy-1",
                        "budget-1",
                        run_id="run-1",
                    ),
                    TokenBudget(limit=10000, budget_id="budget-1").snapshot(),
                )
                if window == "business":
                    artifact = ArtifactRef(
                        artifact_id="plan-1", version="1", content_hash="a" * 64
                    )

                    def seed(state):
                        state.artifacts["plan"] = artifact
                        state.grants[
                            approval_key("report-1", "confirm_plan", artifact)
                        ] = "actor-1"

                    await store.mutate_control(context, seed)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith("RC_")
            }
            env["RC_TEST_DATABASE_URL"] = url
            command = [
                sys.executable,
                "-m",
                "tests.fakes.process_recovery_worker",
                window,
            ]
            with (tmp_path / "child.log").open("wb") as log:
                process = subprocess.Popen(
                    [*command, "start", str(tmp_path)],
                    cwd=Path(__file__).resolve().parents[2],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                async with asyncio.timeout(45):
                    while not (tmp_path / "ready.json").exists():
                        assert process.poll() is None, (
                            tmp_path / "child.log"
                        ).read_text("utf-8", errors="replace")
                        await asyncio.sleep(0.05)
                kill_child(process)
            frozen = json.loads((tmp_path / "ready.json").read_text("utf-8"))
            assert process.returncode != 0
            async with postgres_runtime(url, read_only=True) as (store, saver):
                assert (await store.load_budget(context)).snapshot() == frozen["budget"]
                checkpoint = await saver.aget_tuple(
                    {"configurable": {"thread_id": "v2:report-1"}}
                )
                assert checkpoint is not None
                before = (await store.read_control(context)).model_dump()
            from research_copilot.runtime.runtime_ownership import (
                list_owners,
                recover_owner,
            )

            owners = await list_owners(url)
            assert len(owners) == 1 and owners[0]["connected"] is False
            with pytest.raises(RuntimeError, match="Unsettled runtime"):
                async with postgres_runtime(url):
                    pytest.fail("Killed host was silently replaced")
            await recover_owner(
                url,
                owners[0]["id"],
                actor_id="test-operator",
                note="Tracked process tree was killed and awaited; model is a local fixture with no external execution",
                owner_stopped=True,
            )
            with (tmp_path / "resume.log").open("wb") as log:
                process = subprocess.Popen(
                    [*command, "resume", str(tmp_path)],
                    cwd=Path(__file__).resolve().parents[2],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                async with asyncio.timeout(45):
                    while process.poll() is None:
                        await asyncio.sleep(0.05)
                assert process.returncode == 0, (tmp_path / "resume.log").read_text(
                    "utf-8", errors="replace"
                )
            result = json.loads((tmp_path / "result.json").read_text("utf-8"))
            async with postgres_runtime(url) as (store, saver):
                assert (await store.load_budget(context)).snapshot() == frozen["budget"]
                assert (await store.read_control(context)).model_dump() == before
                if window == "business":
                    assert result["status"] == "completed"
                    assert json.loads(result["last_message"]) == frozen["receipt"]
                    assert len(before["receipts"]) == 1
                else:
                    assert result["status"] == "blocked"
                    assert (
                        "recovery" in result["error"].lower()
                        or "retry" in result["error"].lower()
                    )
                    assert (
                        len((tmp_path / "calls.jsonl").read_text("utf-8").splitlines())
                        == 1
                    )
                    budget = await store.load_budget(context)
                    if window == "sent":
                        key = next(iter(budget.snapshot()["pending"]))
                        await budget.recover_pending(
                            key,
                            actor_id="operator",
                            note="Killed fake provider before response",
                        )
                        assert budget.unknown_requests == 1
                        assert not budget.status()["usage_verified"]
                    else:
                        assert budget.known_actual_tokens == 12 and budget.calls == 1
                        key = next(iter(budget.requests))
                        stale = await store.load_budget(context)
                        await budget.reconcile(
                            key,
                            8,
                            details={
                                "source": "fake_receipt",
                                "actor_id": "operator",
                                "note": "verified downward correction",
                            },
                        )
                        restored = await store.load_budget(context)
                        assert restored.snapshot() == budget.snapshot()
                        assert restored.charged_tokens == 8 and restored.calls == 1
                        await restored.reconcile(key, 8)
                        with pytest.raises(ValueError, match="revision"):
                            await stale.reconcile(key, 6)
        finally:
            if process is not None:
                kill_child(process)
            assert name.startswith("rc_crash_") and name != params.get("dbname")
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
