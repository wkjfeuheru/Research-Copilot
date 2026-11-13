import json

from typer.testing import CliRunner

from research_copilot import cli
from tests.integration.test_run_evaluation import suite


def test_unrun_matrix_can_be_prepared_without_database_or_model_settings(
    tmp_path, monkeypatch
):
    def forbidden():
        raise AssertionError("Preparation must not load connection settings")

    monkeypatch.setattr(cli, "_settings_or_exit", forbidden)
    spec, bindings, output = [
        tmp_path / name for name in ("suite.json", "bindings.json", "result.json")
    ]
    spec.write_text(json.dumps(suite()), encoding="utf-8")
    bindings.write_text(
        json.dumps({"usage_origin": "recorded_runtime_unverified", "runs": []}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "evaluate-runs",
            "--suite",
            str(spec),
            "--bindings",
            str(bindings),
            "--output",
            str(output),
            "--actor-id",
            "actor-1",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text("utf-8"))
    assert report["cases"][0]["reason"] == "not_run"
    assert report["real_model_cost"] is None
    again = CliRunner().invoke(
        cli.app,
        [
            "evaluate-runs",
            "--suite",
            str(spec),
            "--bindings",
            str(bindings),
            "--output",
            str(output),
            "--actor-id",
            "actor-1",
        ],
    )
    assert again.exit_code != 0
    assert json.loads(output.read_text("utf-8")) == report
