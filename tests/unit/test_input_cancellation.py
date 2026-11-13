import asyncio
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
import threading

import pytest

from research_copilot.runtime.session import SessionRecord
from research_copilot.workspace.inputs import WorkspaceFiles


async def test_cancelled_upload_holds_report_lock_until_file_thread_finishes(
    tmp_path, monkeypatch
):
    entered, finish, written = threading.Event(), threading.Event(), threading.Event()
    real_open = Path.open

    @contextmanager
    def delayed_open(path, *args, **kwargs):
        entered.set()
        assert finish.wait(5)
        try:
            with real_open(path, *args, **kwargs) as stream:
                yield stream
        finally:
            written.set()

    def open_file(path, mode="r", *args, **kwargs):
        if path.name == "source.txt" and mode == "xb":
            return delayed_open(path, mode, *args, **kwargs)
        return real_open(path, mode, *args, **kwargs)

    class Store:
        locked = False

        async def load(self, report_id, actor_id):
            return SessionRecord("report-1", "alice", "v2", "policy-1", "budget-1")

        @asynccontextmanager
        async def lock(self, report_id):
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

    store = Store()
    files = WorkspaceFiles(
        store=store, workspace_root=tmp_path, policy_version="policy-1"
    )
    monkeypatch.setattr(Path, "open", open_file)
    task = asyncio.create_task(
        files.import_input("report-1", "alice", "source.txt", b"frozen")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert store.locked and not task.done(), (
            "Report unlocked while its file write was still running"
        )
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(written.wait, 2)
    assert not store.locked
