from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import RepositoryNotFound, TrunksError
from .ids import ObjectId
from .journal import JournalEntry
from .paths import is_ignored, is_internal_path
from .repository import Repository


_DAEMONS: dict[int, subprocess.Popen[bytes]] = {}


@dataclass(frozen=True)
class RuntimeStatus:
    running: bool
    pid: int | None
    path: Path
    socket: Path


def runtime_dir(repo: Repository) -> Path:
    return repo.path.parent / "runtime"


def socket_path(repo: Repository) -> Path:
    return runtime_dir(repo) / "daemon.sock"


def state_path(repo: Repository) -> Path:
    return runtime_dir(repo) / "state.json"


def status(repo: Repository) -> RuntimeStatus:
    pid = _read_pid(repo)
    running = pid is not None and _pid_alive(pid) and socket_path(repo).exists()
    return RuntimeStatus(running=running, pid=pid, path=runtime_dir(repo), socket=socket_path(repo))


def start(repo: Repository, *, interval_seconds: float = 1.0) -> RuntimeStatus:
    _cleanup_stale_runtime(repo)
    current = status(repo)
    if current.running:
        return current
    directory = runtime_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_private(directory)
    with open(directory / "daemon.log", "ab", buffering=0) as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "trunks.runtime",
                "--path",
                str(repo.root),
                "--interval",
                str(interval_seconds),
            ],
            cwd=repo.root,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=_daemon_env(),
        )
    _write_pid(repo, process.pid)
    _DAEMONS[process.pid] = process
    if not _wait_for_socket(repo, process):
        _terminate_process(process)
        raise TrunksError("trunks daemon failed to start")
    return RuntimeStatus(running=True, pid=process.pid, path=directory, socket=socket_path(repo))


def stop(repo: Repository) -> RuntimeStatus:
    current = status(repo)
    if current.running:
        _request_shutdown(repo)
    if current.pid is not None and _pid_alive(current.pid):
        try:
            os.kill(current.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process = _DAEMONS.pop(current.pid, None)
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        else:
            for _ in range(20):
                if not _pid_alive(current.pid):
                    break
                time.sleep(0.05)
    _pid_path(repo).unlink(missing_ok=True)
    socket_path(repo).unlink(missing_ok=True)
    return RuntimeStatus(running=False, pid=current.pid, path=runtime_dir(repo), socket=socket_path(repo))


def scan_once(repo: Repository) -> int:
    previous = _read_watch_state(repo)
    current = _snapshot(repo)
    changed = 0
    for path, oid in sorted(current.items()):
        if previous.get(path) != oid:
            repo.append_journal(JournalEntry.create("worktree", {"op": "write", "path": path, "oid": oid}))
            changed += 1
    for path in sorted(set(previous) - set(current)):
        repo.append_journal(JournalEntry.create("worktree", {"op": "delete", "path": path}))
        changed += 1
    if changed:
        _write_watch_state(repo, current)
    elif not previous and current:
        _write_watch_state(repo, current)
    return changed


def run(path: str, *, interval_seconds: float = 1.0) -> None:
    asyncio.run(run_async(path, interval_seconds=interval_seconds))


async def run_async(path: str, *, interval_seconds: float = 1.0) -> None:
    repo = Repository.find(path)
    directory = runtime_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_private(directory)
    socket_path(repo).unlink(missing_ok=True)
    _write_pid(repo, os.getpid())
    _write_state(repo, "starting")
    shutdown = asyncio.Event()
    server = await asyncio.start_unix_server(lambda reader, writer: _handle_client(repo, reader, writer, shutdown), path=str(socket_path(repo)))
    _chmod_private(socket_path(repo))
    _write_state(repo, "running")
    watcher = asyncio.create_task(_scan_loop(repo, interval_seconds, shutdown))
    try:
        async with server:
            await shutdown.wait()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        server.close()
        await server.wait_closed()
        socket_path(repo).unlink(missing_ok=True)
        _pid_path(repo).unlink(missing_ok=True)
        _write_state(repo, "stopped")


async def _handle_client(
    repo: Repository,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    shutdown: asyncio.Event,
) -> None:
    from .rpc import dispatch_line, is_shutdown_request

    try:
        while not reader.at_eof():
            raw = await reader.readline()
            if not raw:
                break
            response = await dispatch_line(repo, raw.rstrip(b"\n"))
            if response:
                writer.write(response)
                await writer.drain()
            if is_shutdown_request(raw):
                shutdown.set()
                break
    finally:
        writer.close()
        await writer.wait_closed()


async def _scan_loop(repo: Repository, interval_seconds: float, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        scan_once(repo)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trunks.runtime")
    parser.add_argument("--path", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        run(args.path, interval_seconds=args.interval)
    except (KeyboardInterrupt, SystemExit):
        return
    except RepositoryNotFound as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


def _snapshot(repo: Repository) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in sorted(repo.root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = repo._relative_worktree_path(path.resolve(strict=False))
        if is_internal_path(rel) or is_ignored(rel, repo.root):
            continue
        values[repo._normalize_trackable_path(rel)] = str(ObjectId.from_bytes(path.read_bytes()))
    return values


def _watch_state_path(repo: Repository) -> Path:
    return runtime_dir(repo) / "watch-state.json"


def _read_watch_state(repo: Repository) -> dict[str, str]:
    path = _watch_state_path(repo)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _write_watch_state(repo: Repository, state: dict[str, str]) -> None:
    path = _watch_state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _write_state(repo: Repository, state: str) -> None:
    payload = {
        "state": state,
        "pid": os.getpid(),
        "repo": repo.name,
        "root": str(repo.root),
        "socket": str(socket_path(repo)),
        "updated_at": time.time(),
    }
    path = state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _pid_path(repo: Repository) -> Path:
    return runtime_dir(repo) / "daemon.pid"


def _read_pid(repo: Repository) -> int | None:
    path = _pid_path(repo)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _write_pid(repo: Repository, pid: int) -> None:
    path = _pid_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    _chmod_private(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_stale_runtime(repo: Repository) -> None:
    pid = _read_pid(repo)
    if pid is not None and _pid_alive(pid) and _socket_accepts(repo):
        return
    if pid is not None and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _pid_path(repo).unlink(missing_ok=True)
    socket_path(repo).unlink(missing_ok=True)


def _wait_for_socket(repo: Repository, process: subprocess.Popen[bytes], timeout_seconds: float = 5.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if _socket_accepts(repo):
            return True
        time.sleep(0.05)
    return False


def _socket_accepts(repo: Repository) -> bool:
    path = socket_path(repo)
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(path))
        return True
    except OSError:
        return False


def _request_shutdown(repo: Repository) -> None:
    if not socket_path(repo).exists():
        return
    payload = b'{"jsonrpc":"2.0","id":1,"method":"daemon.shutdown","params":{}}\n'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(socket_path(repo)))
            client.sendall(payload)
            client.recv(4096)
    except OSError:
        pass


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


def _daemon_env() -> dict[str, str]:
    # Subprocess runs with cwd=repo.root, which removes the dev source tree
    # from sys.path. Prepend the directory containing the `trunks` package so
    # the daemon can import itself whether or not the wheel is pip-installed.
    env = os.environ.copy()
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_parent}{os.pathsep}{existing}" if existing else package_parent
    return env


if __name__ == "__main__":
    main()
