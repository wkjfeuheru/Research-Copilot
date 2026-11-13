import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_direct_e2e_entrypoint_help_does_not_need_pythonpath():
    script = Path(__file__).resolve().parents[2] / "scripts/e2e_chain.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=script.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--live-authorized" in result.stdout


@pytest.mark.asyncio
async def test_failure_export_preserves_usage_and_marks_incomplete(tmp_path):
    from scripts.e2e_chain import export_failed_run
    from research_copilot.runtime.budget import TokenBudget, BudgetExceeded
    from research_copilot.runtime.business import ControlState
    from research_copilot.runtime.session import SessionRecord

    budget = TokenBudget(limit=2100000, budget_id="r5")
    key = await budget.reserve(100)
    await budget.settle(key, 42)

    class Store:
        async def load(self, report, actor):
            return SessionRecord(report, actor, "v2", "policy", "r5", status="failed")

        async def read_control(self, context):
            return ControlState()

        async def load_budget(self, context):
            return budget

    error = BudgetExceeded(
        "Stage budget exhausted", details={"requested_reservation": 90000}
    )
    await export_failed_run(Store(), "r5", "actor", tmp_path, error)
    record = json.loads((tmp_path / "test_record.json").read_text("utf-8"))
    assert record["budget"]["known_actual_tokens"] == 42
    assert record["acceptance"]["passed"] is False
    assert record["failure"]["details"]["requested_reservation"] == 90000
