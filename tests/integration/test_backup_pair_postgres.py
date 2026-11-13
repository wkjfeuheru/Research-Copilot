"""Reject mismatched backup sources before creating a bundle or running pg_dump."""

import asyncio
import json

import pytest

from research_copilot.runtime import backup as module
from research_copilot.runtime.host_binding import MARKER, bind_host, digest
from research_copilot.runtime.storage import postgres_runtime
from tests.integration.test_maintenance_postgres import databases

__all__ = ["databases"]
pytestmark = pytest.mark.db


@pytest.mark.parametrize(
    "damage", ["wrong_directory", "copied_marker", "missing_marker", "foreign_marker"]
)
async def test_bound_backup_rejects_wrong_workspace_pair(
    databases, tmp_path, monkeypatch, damage
):
    url = databases[0]
    workspace = tmp_path / "workspace"
    async with postgres_runtime(url) as (store, _):
        await bind_host(
            store, workspace, digest({}), actor_id="operator", note="Test binding"
        )
    marker = workspace / MARKER
    selected = workspace
    if damage in {"wrong_directory", "copied_marker"}:
        selected = tmp_path / "other-workspace"
        selected.mkdir()
        if damage == "copied_marker":
            (selected / MARKER).write_bytes(marker.read_bytes())
    elif damage == "missing_marker":
        marker.unlink()
    else:
        value = json.loads(marker.read_text("utf-8"))
        value["workspace_id"] = "0" * 32
        marker.write_text(json.dumps(value), encoding="utf-8")
    await assert_rejected_before_output(url, selected, tmp_path, monkeypatch)


async def test_unbound_database_rejects_marked_workspace(
    databases, tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / MARKER).write_text(
        json.dumps(
            {"format": 1, "workspace_id": "1" * 32, "database_identity": "2" * 64}
        ),
        encoding="utf-8",
    )
    await assert_rejected_before_output(databases[0], workspace, tmp_path, monkeypatch)


async def assert_rejected_before_output(url, workspace, tmp_path, monkeypatch):
    calls = []

    def unexpected_tool(*args):
        calls.append(args)
        raise AssertionError("Mismatched source reached pg_dump")

    monkeypatch.setattr(module, "_pg_tool", unexpected_tool)
    destination = tmp_path / "bundle"
    with pytest.raises(ValueError, match="(?i)(binding|marker|workspace)"):
        await asyncio.to_thread(module.backup, url, workspace, destination)
    assert not destination.exists()
    assert not calls
