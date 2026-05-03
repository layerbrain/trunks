from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator

from trunks.repository import Repository

from .storage import load_run

WATCH_EVENT_SCHEMA = "trunks.actions.watch_event.v1"
TERMINAL_PHASES = {"succeeded", "failed", "canceled", "skipped"}


def watch_run(
    repo: Repository,
    run: str,
    *,
    interval_s: float = 0.25,
    timeout_s: float | None = None,
) -> Iterator[dict[str, object]] | AsyncIterator[dict[str, object]]:
    if _in_running_loop():
        return watch_run_async(repo, run, interval_s=interval_s, timeout_s=timeout_s)
    return _watch_run_sync(repo, run, interval_s=interval_s, timeout_s=timeout_s)


async def watch_run_async(
    repo: Repository,
    run: str,
    *,
    interval_s: float = 0.25,
    timeout_s: float | None = None,
) -> AsyncIterator[dict[str, object]]:
    started = time.monotonic()
    seen_logs: set[tuple[object, object]] = set()
    last_phase: str | None = None
    while True:
        payload = load_run(repo, run)
        state = payload.get("state")
        phase = state.get("phase") if isinstance(state, dict) and isinstance(state.get("phase"), str) else "unknown"
        phase_changed = phase != last_phase
        if phase_changed and phase not in TERMINAL_PHASES:
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "action_state",
                "run": run,
                "phase": phase,
                "state": state or {},
            }
        logs = payload.get("logs")
        if isinstance(logs, list):
            for item in logs:
                if not isinstance(item, dict):
                    continue
                key = (item.get("stream"), item.get("seq"))
                if key in seen_logs:
                    continue
                seen_logs.add(key)
                yield {
                    "_schema_version": WATCH_EVENT_SCHEMA,
                    "object": "action_log",
                    "run": run,
                    **item,
                }
        if phase_changed and phase in TERMINAL_PHASES:
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "action_state",
                "run": run,
                "phase": phase,
                "state": state or {},
            }
        if phase_changed:
            last_phase = phase
        if phase in TERMINAL_PHASES:
            result = payload.get("result")
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "action_result",
                "run": run,
                "phase": phase,
                "result": result,
            }
            return
        if timeout_s is not None and time.monotonic() - started >= timeout_s:
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "watch_timeout",
                "run": run,
                "phase": phase,
            }
            return
        await asyncio.sleep(interval_s)


def _watch_run_sync(
    repo: Repository,
    run: str,
    *,
    interval_s: float,
    timeout_s: float | None,
) -> Iterator[dict[str, object]]:
    started = time.monotonic()
    seen_logs: set[tuple[object, object]] = set()
    last_phase: str | None = None
    while True:
        payload = load_run(repo, run)
        state = payload.get("state")
        phase = state.get("phase") if isinstance(state, dict) and isinstance(state.get("phase"), str) else "unknown"
        phase_changed = phase != last_phase
        if phase_changed and phase not in TERMINAL_PHASES:
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "action_state",
                "run": run,
                "phase": phase,
                "state": state or {},
            }
        logs = payload.get("logs")
        if isinstance(logs, list):
            for item in logs:
                if not isinstance(item, dict):
                    continue
                key = (item.get("stream"), item.get("seq"))
                if key in seen_logs:
                    continue
                seen_logs.add(key)
                yield {
                    "_schema_version": WATCH_EVENT_SCHEMA,
                    "object": "action_log",
                    "run": run,
                    **item,
                }
        if phase_changed and phase in TERMINAL_PHASES:
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "action_state",
                "run": run,
                "phase": phase,
                "state": state or {},
            }
        if phase_changed:
            last_phase = phase
        if phase in TERMINAL_PHASES:
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "action_result",
                "run": run,
                "phase": phase,
                "result": payload.get("result"),
            }
            return
        if timeout_s is not None and time.monotonic() - started >= timeout_s:
            yield {
                "_schema_version": WATCH_EVENT_SCHEMA,
                "object": "watch_timeout",
                "run": run,
                "phase": phase,
            }
            return
        time.sleep(interval_s)


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
