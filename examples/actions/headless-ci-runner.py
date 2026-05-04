from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from trunks.actions.run import enqueue_command
from trunks.actions.watch import watch_run_async
from trunks.actions.executor import execute_once
from trunks.repository import Repository


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Queue one Trunks Actions job, run one executor, and stream logs.")
    parser.add_argument("--repo", default=".", help="Trunks repository root")
    parser.add_argument("--executor", default="headless-ci-executor")
    parser.add_argument("--command", default="python3.12 -m unittest discover")
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve()
    repo = Repository.find(root)
    run = enqueue_command(repo, args.command)
    executor_task = asyncio.create_task(execute_once(repo, executor=args.executor, cwd=str(root)))

    async for event in watch_run_async(repo, run.id, interval_s=0.05):
        if event.get("object") == "action_log":
            stream = sys.stderr if event.get("stream") == "stderr" else sys.stdout
            print(str(event.get("text", "")), end="", file=stream, flush=True)
        elif event.get("object") == "action_result":
            break

    result = await executor_task
    if not isinstance(result, dict):
        return 1
    state = result.get("state")
    run_result = result.get("result")
    phase = state.get("phase") if isinstance(state, dict) else "failed"
    if phase != "succeeded":
        return 1
    if isinstance(run_result, dict) and isinstance(run_result.get("exit_code"), int):
        return int(run_result["exit_code"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
