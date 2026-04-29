"""Detached supervisor for `trunks mount`.

Spawned by the CLI as a new-session subprocess. Boots the NFS server, runs the
OS-level `mount` command to attach the kernel client, writes the runtime
record file the supervisor uses to terminate cleanly, and waits for SIGTERM.

Usage (internal): python -m trunks.mount.daemon --repo-path <p> --target <t> --export <name>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

from ..repository import Repository
from .lifecycle import MountError, MountInfo, mount_nfs, unmount_nfs
from .server import NfsServer

logger = logging.getLogger("trunks.mount.daemon")


async def _run(repo_path: Path, target: Path, export_name: str, runtime_file: Path) -> int:
    repo = Repository.find(repo_path)
    server = NfsServer(repo, export_name=export_name)
    await server.start()

    info = MountInfo(target=target, host=server.host, port=server.port, export=f"/{export_name}")
    try:
        await mount_nfs(info)
    except MountError as err:
        logger.error("mount failed: %s", err)
        await server.stop()
        runtime_file.unlink(missing_ok=True)
        return 1
    server.disable_mount_protocol()

    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": server.host,
                "port": server.port,
                "export": info.export,
                "target": str(target),
            }
        ),
        encoding="utf-8",
    )
    runtime_file.chmod(0o600)

    stop = asyncio.Event()

    def _request_stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_stop)

    serve_task = asyncio.create_task(server.serve_forever())
    target_task = asyncio.create_task(_watch_mount_target(target, runtime_file, stop))
    await stop.wait()

    try:
        await unmount_nfs(target, force=True)
    except MountError as err:
        logger.warning("unmount during shutdown failed: %s", err)
    await server.stop()
    for task in (serve_task, target_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    runtime_file.unlink(missing_ok=True)
    return 0


async def _watch_mount_target(target: Path, runtime_file: Path, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(1.0)
        if not target.exists():
            logger.warning("mount target disappeared: %s", target)
            stop.set()
            return
        if not runtime_file.exists():
            logger.warning("runtime file disappeared: %s", runtime_file)
            stop.set()
            return


def main() -> int:
    parser = argparse.ArgumentParser(prog="trunks-mount-daemon")
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--export", required=True)
    parser.add_argument("--runtime-file", required=True)
    args = parser.parse_args()

    return asyncio.run(
        _run(
            Path(args.repo_path),
            Path(args.target),
            args.export,
            Path(args.runtime_file),
        )
    )


if __name__ == "__main__":
    sys.exit(main())
