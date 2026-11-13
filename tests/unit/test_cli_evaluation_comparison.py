import json
from copy import deepcopy

import pytest
from typer.testing import CliRunner

from research_copilot import cli
from tests.unit.test_evaluation_comparison import report


@pytest.mark.parametrize(
    "status,code", [("no_regression", 0), ("regressed", 2), ("not_comparable", 3)]
)
def test_comparison_cli_preserves_failed_evidence_and_never_connects(
    tmp_path, monkeypatch, status, code
):
    def forbidden():
        raise AssertionError("Comparison must not load host settings")

    monkeypatch.setattr(cli, "_settings_or_exit", forbidden)
    spec, before = report()
    after = deepcopy(before)
    if status == "regressed":
        after["cases"][0].update(completed=False, signed=False, quality_passed=False)
    elif status == "not_comparable":
        after["cases"][0]["usage_verified"] = False
    paths = [tmp_path / (name + ".json") for name in ("suite", "before", "after")]
    for path, data in zip(paths, (spec, before, after), strict=True):
        path.write_text(json.dumps(data), encoding="utf-8")
    original = [path.read_bytes() for path in paths]
    output = tmp_path / "comparison.json"
    args = [
        "compare-evaluations",
        "--suite",
        str(paths[0]),
        "--baseline",
        str(paths[1]),
        "--candidate",
        str(paths[2]),
        "--output",
        str(output),
    ]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == code, result.output
    assert json.loads(output.read_text("utf-8"))["status"] == status
    assert original == [path.read_bytes() for path in paths]
    # Refuse overwriting either a previous result or an input file.
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1
    result = CliRunner().invoke(cli.app, [*args[:-1], str(paths[1])])
    assert result.exit_code == 1
    assert original == [path.read_bytes() for path in paths]
