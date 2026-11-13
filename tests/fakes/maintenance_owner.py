"""Isolated PG access owner for actual process-exit acceptance; no models."""

import asyncio
import os
from pathlib import Path
import sys

from research_copilot.runtime.loop import run_async
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.maintenance import maintenance_connection


async def main():
    root = Path(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "maintenance":
        with maintenance_connection(os.environ["RC_TEST_DATABASE_URL"]):
            (root / "ready").write_text("connected", encoding="utf-8")
            while not (root / "stop").exists():
                await asyncio.sleep(0.05)
        (root / "closed").write_text("maintenance closed", encoding="utf-8")
        return
    async with postgres_runtime(os.environ["RC_TEST_DATABASE_URL"]):
        (root / "ready").write_text("connected", encoding="utf-8")
        while not (root / "stop").exists():
            await asyncio.sleep(0.05)
    (root / "closed").write_text("dependencies and lock closed", encoding="utf-8")


if __name__ == "__main__":
    run_async(main())
