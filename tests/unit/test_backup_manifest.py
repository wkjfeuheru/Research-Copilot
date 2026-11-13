import json

import pytest

from research_copilot.runtime.migrations import SCHEMA_VERSION

from research_copilot.runtime.backup import (
    file_inventory,
    verify_bundle,
    workspace_lease,
)


def test_backup_client_failure_preserves_diagnosis_but_redacts_credentials(monkeypatch):
    from subprocess import CompletedProcess
    from research_copilot.runtime.backup import _pg_tool

    def run(arguments, **kwargs):
        assert kwargs["env"]["PGHOST"] == "127.0.0.1"
        assert "private-pass" not in " ".join(arguments)
        return CompletedProcess(
            arguments, 1, stdout=b"", stderr=b"pg_dump: permission denied; private-pass"
        )

    monkeypatch.setattr("research_copilot.runtime.backup.subprocess.run", run)
    with pytest.raises(RuntimeError, match="permission denied") as error:
        _pg_tool(
            ["pg_dump", "--version"],
            "postgresql://test:private-pass@127.0.0.1:5432/test",
        )
    assert "private-pass" not in str(error.value)


def test_bundle_detects_modified_missing_and_extra_files(tmp_path):
    root = tmp_path / "backup"
    (root / "workspace/report-1").mkdir(parents=True)
    (root / "workspace/report-1/report.md").write_text("frozen", encoding="utf-8")
    (root / "database.dump").write_bytes(b"test-dump")
    manifest = {
        "format": 1,
        "schema_version": SCHEMA_VERSION,
        "files": file_inventory(root),
        "database": {},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_bundle(root) == manifest
    (root / "workspace/report-1/report.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        verify_bundle(root)


def test_host_lease_refuses_concurrent_backup_or_host(tmp_path):
    from filelock import Timeout

    with workspace_lease(tmp_path / "workspace"):
        with pytest.raises(Timeout):
            with workspace_lease(tmp_path / "workspace"):
                pytest.fail("acquired active workspace")


def test_bundle_rejects_unexpected_format_and_parent_link(tmp_path):
    root = tmp_path / "bad"
    root.mkdir()
    (root / "manifest.json").write_text('{"format": 999}', encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        verify_bundle(root)
