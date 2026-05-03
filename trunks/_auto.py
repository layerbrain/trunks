from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


def run_auto(coro_factory: Callable[[], Awaitable[T]]) -> T | Awaitable[T]:
    if _in_running_loop():
        return coro_factory()
    return asyncio.run(coro_factory())


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
