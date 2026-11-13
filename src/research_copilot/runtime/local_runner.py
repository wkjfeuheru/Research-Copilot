"""Run only one submitted CLI command under shared durable admission."""

import asyncio

from research_copilot.runtime.run_worker import RunWorker
from research_copilot.runtime.runtime_watch import require_live


async def run_command(queue, session, job):
    worker = RunWorker(queue, session, job_id=job["id"], max_local=1)
    try:
        await worker.start()
        while True:
            require_live(getattr(session, "store", None))
            current = await queue.get(job["id"], job["actor_id"])
            if current["status"] == "succeeded":
                return await session.get_status(job["report_id"], job["actor_id"])
            if current["status"] in {"failed", "cancelled", "recovery_required"}:
                raise RuntimeError(
                    f"Research command {current['id']} stopped: {current['status']} ({current['error'] or ''})"
                )
            await asyncio.sleep(0.1)
    finally:
        try:
            await worker.aclose()
        except TimeoutError:
            # Preserve the host's model/database lifetime through settlement,
            # just as the Web lifespan does. A second interrupt can kill the
            # process; then the persisted request requires explicit recovery.
            await worker.drain()
