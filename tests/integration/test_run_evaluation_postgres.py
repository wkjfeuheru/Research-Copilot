"""Actual PostgreSQL read-only collector, using disposable fixture databases."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.run_evaluation import evaluate_runs
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import postgres_runtime
from tests.integration.test_maintenance_postgres import databases
from tests.integration.test_run_evaluation import bindings, suite

__all__ = ["databases"]
pytestmark = pytest.mark.db


async def test_collector_uses_readonly_transactions_without_application_writes(
    databases,
    tmp_path,
    monkeypatch,
):
    url = databases[0]
    async with postgres_runtime(url) as (store, _):
        await store.create(
            SessionRecord(
                "report-1",
                "actor-1",
                "v2",
                "policy-1",
                "budget-1",
                run_id="run-1",
                status="failed",
            ),
            TokenBudget(limit=1000000, budget_id="budget-1").snapshot(),
        )
        before = await store.read_snapshot("report-1", "actor-1")
    async with postgres_runtime(url, read_only=True) as (store, _):
        async with store.engine.connect() as connection:
            assert (
                await connection.execute(text("SHOW transaction_read_only"))
            ).scalar_one() == "on"
        result = await evaluate_runs(store, "actor-1", suite(), bindings())
        after = await store.read_snapshot("report-1", "actor-1")
        assert result["cases"][0]["session_status"] == "failed"
        assert result["budget_curve"][0]["agent_completion_rate"] == 0
        assert before.record == after.record and before.state == after.state
        assert before.budget.snapshot() == after.budget.snapshot()
        with pytest.raises(LookupError):
            await evaluate_runs(store, "other-actor", suite(), bindings())

    from research_copilot import cli

    monkeypatch.setattr(
        cli, "_settings_or_exit", lambda: SimpleNamespace(database_url=url)
    )
    spec, bound, output = [
        tmp_path / name for name in ("suite.json", "bindings.json", "result.json")
    ]
    spec.write_text(json.dumps(suite()), encoding="utf-8")
    bound.write_text(json.dumps(bindings()), encoding="utf-8")
    cli_result = await asyncio.to_thread(
        CliRunner().invoke,
        cli.app,
        [
            "evaluate-runs",
            "--suite",
            str(spec),
            "--bindings",
            str(bound),
            "--output",
            str(output),
            "--actor-id",
            "actor-1",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    exported = json.loads(output.read_text("utf-8"))
    assert (
        exported["cases"][0]["snapshot_sha256"] == result["cases"][0]["snapshot_sha256"]
    )
