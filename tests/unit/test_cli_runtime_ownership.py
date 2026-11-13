from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from research_copilot import cli
from research_copilot.runtime import runtime_ownership, maintenance_ownership, storage


@pytest.mark.parametrize(
    "module,list_command,recover_command",
    [
        (runtime_ownership, "runtime-owners", "recover-runtime"),
        (maintenance_ownership, "maintenance-owners", "recover-maintenance"),
    ],
)
def test_runtime_recovery_cli_keeps_confirmation_and_evidence_separate(
    monkeypatch, module, list_command, recover_command
):
    calls = []
    monkeypatch.setattr(
        cli, "_settings_or_exit", lambda: SimpleNamespace(database_url="isolated")
    )

    async def list_owners(url):
        assert url == "isolated"
        return [{"id": "a" * 32, "connected": False}]

    async def recover(url, owner_id, **kwargs):
        calls.append((owner_id, kwargs))
        if not kwargs["owner_stopped"]:
            raise ValueError("Old process must be confirmed stopped")
        return {"id": owner_id, "state": "recovered"}

    monkeypatch.setattr(module, "list_owners", list_owners)
    monkeypatch.setattr(module, "recover_owner", recover)
    runner = CliRunner()
    listed = runner.invoke(cli.app, [list_command])
    assert listed.exit_code == 0 and '"connected": false' in listed.output
    arguments = [
        recover_command,
        "--owner-id",
        "a" * 32,
        "--actor-id",
        "operator",
        "--note",
        "Process and external work inspected",
    ]
    refused = runner.invoke(cli.app, arguments)
    assert refused.exit_code == 1 and "stopped" in refused.output
    released = runner.invoke(cli.app, [*arguments, "--owner-stopped"])
    assert released.exit_code == 0 and '"state": "recovered"' in released.output
    assert calls[-1][1] == dict(
        actor_id="operator",
        note="Process and external work inspected",
        owner_stopped=True,
    )


def test_cli_status_uses_database_enforced_read_only_access(monkeypatch):
    import research_copilot.web.status as status_module

    captured = {}
    monkeypatch.setattr(
        cli, "_settings_or_exit", lambda: SimpleNamespace(database_url="isolated")
    )

    @asynccontextmanager
    async def runtime(url, **kwargs):
        captured.update(kwargs)
        yield object(), None

    async def status(*args):
        return {"report_id": "report-1"}

    monkeypatch.setattr(storage, "postgres_runtime", runtime)
    monkeypatch.setattr(status_module, "status_payload", status)
    result = CliRunner().invoke(
        cli.app, ["status", "--report-id", "report-1", "--actor-id", "actor-1"]
    )
    assert result.exit_code == 0, result.output
    assert captured["read_only"] is True
