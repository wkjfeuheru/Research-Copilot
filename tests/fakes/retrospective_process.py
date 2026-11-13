"""Disposable PG worker for OS-kill lease tests; never constructs a model."""

import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.runtime.experience import RetrospectiveWorker
from research_copilot.runtime.loop import run_async
from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.runtime.urls import to_sqlalchemy_url


async def main(root):
    engine = create_async_engine(to_sqlalchemy_url(os.environ["RC_TEST_DATABASE_URL"]))
    store = SqlSessionStore(engine)

    class Extractor:
        async def extract(self, materials):
            (root / "entered").write_text(str(os.getpid()), encoding="utf-8")
            await asyncio.Event().wait()

    worker = RetrospectiveWorker(
        store, Extractor(), lease_seconds=2, poll_interval=0.05
    )
    try:
        await worker.start()
        (root / "ready").write_text(str(os.getpid()), encoding="utf-8")
        await asyncio.Event().wait()
    finally:
        await worker.stop()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    run_async(main(parser.parse_args().directory))
