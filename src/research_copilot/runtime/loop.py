"""Event-loop selection shared by the host and tests.

Windows must use SelectorEventLoop so psycopg async mode and MCP stdio share one loop.
"""

import asyncio
import sys
from typing import Callable, Coroutine, Literal, TypeVar

T = TypeVar("T")

LoopPreference = Literal["auto", "selector", "proactor"]
LOOP_PREFERENCES: tuple[str, ...] = ("auto", "selector", "proactor")
LoopFactory = Callable[[], asyncio.AbstractEventLoop]


def serve_loop() -> asyncio.AbstractEventLoop:
    """Uvicorn loop factory. Windows must not use ProactorEventLoop with psycopg."""

    factory = main_loop_factory("auto")
    if factory is None:
        return asyncio.new_event_loop()
    return factory()


def main_loop_factory(preference: LoopPreference = "auto") -> LoopFactory | None:
    """Return a loop factory, or None to keep the platform default."""

    if preference == "selector":
        return asyncio.SelectorEventLoop
    if preference == "proactor":
        proactor = getattr(asyncio, "ProactorEventLoop", None)
        if proactor is None:
            raise ValueError("当前平台没有 ProactorEventLoop")
        return proactor
    if preference != "auto":
        raise ValueError(
            f"未知的事件循环偏好：{preference}；可选：{'/'.join(LOOP_PREFERENCES)}"
        )
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def run_async(
    main: Coroutine[object, object, T],
    *,
    loop_preference: LoopPreference = "auto",
) -> T:
    """Run a coroutine on the selected loop and close that loop."""

    factory = main_loop_factory(loop_preference)
    if factory is None:
        return asyncio.run(main)
    return asyncio.run(main, loop_factory=factory)
