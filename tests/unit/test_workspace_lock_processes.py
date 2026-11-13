import os
from pathlib import Path
import subprocess
import sys
import time

from filelock import Timeout
import pytest

from research_copilot.runtime.workspace_lock import workspace_lease


def test_independent_shared_holders_block_maintenance_until_last_process_exits(
    tmp_path,
):
    processes = []
    root = tmp_path / "workspace"

    def launch(label, mode, probe=False):
        signal = tmp_path / label
        process = subprocess.Popen(
            [
                sys._base_executable,
                "-m",
                "tests.fakes.workspace_lock_holder",
                str(root),
                str(signal),
                mode,
            ]
            + (["probe"] if probe else []),
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        deadline = time.monotonic() + 30
        while not signal.exists():
            assert process.poll() is None, process.stderr.read().decode(
                errors="replace"
            )
            assert time.monotonic() < deadline
            time.sleep(0.02)
        value = signal.read_text("utf-8")
        if not probe:
            assert value == str(process.pid)
        return process, value

    try:
        first, _ = launch("first", "shared")
        second, _ = launch("second", "shared")
        with pytest.raises(Timeout):
            with workspace_lease(root):
                pytest.fail("Maintenance overlapped two independent hosts")
        first.kill()
        first.wait(timeout=10)
        with pytest.raises(Timeout):
            with workspace_lease(root):
                pytest.fail("Killing one reader released another reader's lock")
        second.kill()
        second.wait(timeout=10)
        with workspace_lease(root):
            third, result = launch("third", "shared", probe=True)
            third.wait(timeout=10)
            assert result == "blocked"
        legacy, _ = launch("legacy", "legacy")
        with pytest.raises(Timeout):
            with workspace_lease(root, shared=True):
                pytest.fail("New protocol ignored old process")
        legacy.kill()
        legacy.wait(timeout=10)
        with workspace_lease(root, shared=True):
            peer, result = launch("legacy-probe", "legacy", probe=True)
            peer.wait(timeout=10)
            assert result == "blocked"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stderr.close()
