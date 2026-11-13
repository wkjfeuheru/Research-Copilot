from contextlib import asynccontextmanager
import os

from filelock import FileLock, Timeout
import pytest

from research_copilot import host
from research_copilot.runtime.backup import workspace_lease


async def test_two_runtime_hosts_share_workspace_but_maintenance_is_excluded(
    tmp_path, monkeypatch
):
    @asynccontextmanager
    async def session(*args, **kwargs):
        yield "fixture"

    monkeypatch.setattr(host, "_open_session", session)
    root = tmp_path / "workspace"
    async with host.open_session(None, workspace_root=root):
        async with host.open_session(None, workspace_root=root):
            with pytest.raises(Timeout):
                async with host.open_session(
                    None, workspace_root=root, initialize=True
                ):
                    pytest.fail("Initialization shared a live workspace")
            with pytest.raises(Timeout):
                with workspace_lease(root):
                    pytest.fail("Maintenance entered with two live hosts")
    with workspace_lease(root):
        with pytest.raises(Timeout):
            async with host.open_session(None, workspace_root=root):
                pytest.fail("Runtime entered during maintenance")


@pytest.mark.parametrize("shared", [False, True])
def test_native_workspace_lock_interoperates_with_legacy_filelock(tmp_path, shared):
    root = tmp_path / "workspace"
    path = str(root) + ".host.lock"
    with FileLock(path, timeout=0):
        with pytest.raises(Timeout):
            with workspace_lease(root, shared=shared):
                pytest.fail("New protocol bypassed legacy holder")
    with workspace_lease(root, shared=shared):
        with pytest.raises(Timeout):
            with FileLock(path, timeout=0):
                pytest.fail("Legacy protocol bypassed new holder")


def test_workspace_lock_rejects_hardlinked_sidecar_without_changing_target(tmp_path):
    root = tmp_path / "workspace"
    target = tmp_path / "evidence"
    target.write_bytes(b"unchanged")
    path = tmp_path / "workspace.host.lock"
    os.link(target, path)
    with pytest.raises(ValueError, match="lock"):
        with workspace_lease(root):
            pytest.fail("Hardlinked lock was accepted")
    assert target.read_bytes() == b"unchanged"
