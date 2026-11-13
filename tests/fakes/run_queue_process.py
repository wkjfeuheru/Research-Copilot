"""Queue-only OS process harness. Does not construct a graph or provider client."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.runtime.loop import run_async
from research_copilot.runtime.run_queue import RunQueue
from research_copilot.runtime.urls import to_sqlalchemy_url


async def main(directory, start):
    engine = create_async_engine(to_sqlalchemy_url(os.environ["RC_TEST_DATABASE_URL"]))
    queue = RunQueue(engine)
    try:
        (directory / "ready").write_text(str(os.getpid()), encoding="utf-8")
        claim = None
        while claim is None:
            claim = await queue.claim(f"process-{os.getpid()}", lease_seconds=1.2)
            if claim is None:
                await asyncio.sleep(0.05)
        if start:
            claim = await queue.start(claim)
        (directory / "claim.json").write_text(
            json.dumps(claim, default=str), encoding="utf-8"
        )
        while True:
            await asyncio.sleep(0.2)
            await queue.renew(claim, lease_seconds=1.2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--start", action="store_true")
    options = parser.parse_args()
    run_async(main(options.directory, options.start))
