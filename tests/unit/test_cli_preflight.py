from typer.testing import CliRunner
from research_copilot.cli import app


def test_preflight_test_mode_is_explicit_and_production_blocks_example_rules():
    runner = CliRunner()
    denied = runner.invoke(app, ["preflight"])
    assert denied.exit_code != 0
    assert "example" in str(denied.exception)
    allowed = runner.invoke(app, ["preflight", "--mode", "test"])
    assert allowed.exit_code == 0, allowed.exception
    assert '"mode": "test"' in allowed.stdout


def test_read_only_commands_and_explicit_recovery_do_not_require_models_config():
    runner = CliRunner()
    for name in ("status", "cancel", "recover-rework"):
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0
    assert runner.invoke(app, ["start", "--help"]).exit_code == 0
