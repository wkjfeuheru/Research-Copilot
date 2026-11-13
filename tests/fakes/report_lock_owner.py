"""Real managed host/report lock process; starts no models or external execution."""

import asyncio
import os
from pathlib import Path
import sys

from research_copilot.runtime.loop import run_async
from research_copilot.runtime.storage import postgres_runtime


async def main():
    root = Path(sys.argv[1])
    async with postgres_runtime(os.environ["RC_TEST_DATABASE_URL"]) as (store, _):
        async with store.lock("report-1"):
            (root / "ready").write_text("locked", encoding="utf-8")
            while not (root / "stop").exists():
                await asyncio.sleep(0.05)
    (root / "closed").write_text("closed", encoding="utf-8")


if __name__ == "__main__":
    run_async(main())
