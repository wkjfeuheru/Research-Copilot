import pytest


def metric(key, value, inputs):
    return {
        "key": key,
        "value": value,
        "unit": "pp",
        "semantic_type": "rate_difference",
        "formula_id": "difference",
        "input_keys": inputs,
        "numerator_basis": "a",
        "denominator_basis": "b",
        "display_precision": 2,
        "is_estimate": False,
    }


def test_batch_feedback_names_all_wrong_metrics_and_missing_required_semantics():
    from research_copilot.subagents.metric_quality import validate_metric_batch

    inputs = {"a": {"raw_value": "1.00"}, "b": {"raw_value": "2.00"}}
    with pytest.raises(ValueError) as error:
        validate_metric_batch(
            [metric("first", "1.00", ["a", "b"]), metric("second", "2.00", ["a", "b"])],
            inputs,
            requirements=[
                {"semantic_type": "growth_gap", "unit": "pp", "value": "1.65"}
            ],
        )
    message = str(error.value)
    assert "first" in message and "second" in message and "growth_gap" in message
    assert "expected=-1.00" in message


def test_batch_returns_validated_dependency_metadata():
    from research_copilot.subagents.metric_quality import validate_metric_batch

    result = validate_metric_batch(
        [
            metric("first", "-1.00", ["a", "b"]),
            metric("second", "-2.00", ["first", "a"]),
        ],
        {"a": {"raw_value": "1.00", "is_estimate": True}, "b": {"raw_value": "2.00"}},
    )
    assert result["second"]["is_estimate"] is True


def test_recovery_feedback_checks_immutable_output_hash(tmp_path):
    from research_copilot.subagents.source_context import latest_execution_feedback
    from research_copilot.runtime.business import ControlState

    state = ControlState(
        role_data={
            "executions": {
                "e": {
                    "execution_id": "e",
                    "dispatch_id": "d",
                    "status": "invalid_result",
                    "started_at": 1,
                    "root": "artifacts/d/executions/e",
                    "files": {"artifacts/d/executions/e/output.tar": "wrong"},
                }
            }
        }
    )
    output = tmp_path / "artifacts/d/executions/e/output.tar"
    output.parent.mkdir(parents=True)
    output.write_text("{}")
    with pytest.raises(ValueError, match="hash"):
        latest_execution_feedback(state, "d", tmp_path)


def test_batch_reports_formula_and_fact_lineage_together():
    from research_copilot.subagents.metric_quality import validate_metric_batch

    inputs = {
        "a": {"raw_value": "1.00", "assumption_key": "a"},
        "b": {"raw_value": "2.00", "assumption_key": "b"},
    }
    with pytest.raises(ValueError) as error:
        validate_metric_batch(
            [metric("bad", "9", ["a", "b"])], inputs, require_fact_lineage=True
        )
    assert "formula/value mismatch" in str(error.value)
    assert "bad: Metric requires at least one authorized fact input" in str(error.value)


def test_batch_propagates_fact_lineage_through_derived_metrics():
    from research_copilot.subagents.metric_quality import validate_metric_batch

    result = validate_metric_batch(
        [metric("first", "-1", ["a", "b"]), metric("second", "-2", ["first", "a"])],
        {
            "a": {"raw_value": "1.00", "evidence_id": "E-1"},
            "b": {"raw_value": "2.00", "assumption_key": "b"},
        },
        require_fact_lineage=True,
    )
    assert any(i.get("evidence_id") == "E-1" for i in result["second"]["inputs"])


def test_frozen_requirement_checks_the_matching_metric_value():
    from research_copilot.subagents.metric_quality import validate_metric_batch

    inputs = {"a": {"raw_value": "1.00"}, "b": {"raw_value": "2.00"}}
    metrics = [metric("change", "-1.00", ["a", "b"])]
    requirement = {"semantic_type": "rate_difference", "unit": "pp", "value": "-1.00"}
    assert (
        validate_metric_batch(metrics, inputs, requirements=[requirement])["change"][
            "value"
        ]
        == "-1.00"
    )
    with pytest.raises(ValueError, match="Missing required metric"):
        validate_metric_batch(
            metrics, inputs, requirements=[{**requirement, "value": "2.00"}]
        )
