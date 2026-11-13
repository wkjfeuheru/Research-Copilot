import pytest
from scripts.acceptance_budget import CallBudget


def test_budget_survives_restart_and_reserves_before_call(tmp_path):
    path = tmp_path / "budget.json"
    first = CallBudget(path, {"model": 1, "cnequity": 2})
    first.reserve("model")
    restarted = CallBudget(path, {"model": 1, "cnequity": 2})
    with pytest.raises(RuntimeError, match="budget"):
        restarted.reserve("model")
    restarted.reserve("cnequity")
    assert restarted.state["used"] == {"model": 1, "cnequity": 1}


def test_budget_cannot_be_silently_raised(tmp_path):
    path = tmp_path / "budget.json"
    CallBudget(path, {"model": 1})
    with pytest.raises(RuntimeError, match="limits"):
        CallBudget(path, {"model": 12})
