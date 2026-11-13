"""Drain already-admitted local side effects before propagating cancellation."""

import asyncio
from contextlib import suppress


async def await_completion(awaitable):
    """Do not abandon an in-flight lock query or file thread on repeated cancellation.

    This may wait indefinitely for uncooperative work. Use only where releasing
    ownership early is unsafe, not to shield provider calls or start new work.
    """
    task = asyncio.create_task(awaitable)
    interrupted = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            interrupted = exc
        except Exception:
            break
    if interrupted is not None:
        with suppress(BaseException):
            task.result()
        raise interrupted
    return task.result()
