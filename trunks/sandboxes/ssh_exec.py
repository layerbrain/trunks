from __future__ import annotations

import asyncio
import re
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Literal, cast

from .provider import (
    Event,
    ExecResult,
    HydrateProgress,
    LogChunk,
    Sandbox,
    SandboxFile,
    SandboxRequest,
    TransferProgress,
)


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SshExecSandbox(Sandbox):
    """SSH-based sandbox runtime shared by DigitalOcean droplets and Prime Intellect pods.

    Subclasses provide their own destroy() to release provider-specific resources
    (droplet + SSH key for DO, pod for PI). Everything else — hydrate, run, upload,
    download, cancel — is handled here using a single SSH binary process per call.
    """

    def __init__(
        self,
        request: SandboxRequest,
        *,
        ip: str,
        private_key_path: Path,
        workspace: str,
        ssh_user: str = "root",
        ssh_port: int = 22,
        connect_timeout_s: int = 10,
        host_label: str | None = None,
    ) -> None:
        self.request = request
        self.ip = ip
        self.private_key_path = private_key_path
        self.workspace = workspace
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.connect_timeout_s = connect_timeout_s
        self.host_label = host_label or "remote"
        self._process: asyncio.subprocess.Process | None = None

    async def hydrate(self) -> AsyncIterator[HydrateProgress]:
        await self._run_control_command(f"mkdir -p {shlex.quote(self.workspace)}")
        yield HydrateProgress(bytes_total=0, bytes_done=0, objects_total=0, objects_done=0)

    async def workspace_root(self) -> str:
        return self.workspace

    async def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_s: int,
    ) -> AsyncIterator[Event]:
        started = time.monotonic()
        remote_command = "sh -s -- " + " ".join(shlex.quote(part) for part in command)
        script = _remote_script(cwd, env).encode()
        self._process = await asyncio.create_subprocess_exec(
            *self._ssh_args(remote_command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._process.stdin is not None
        self._process.stdin.write(script)
        await self._process.stdin.drain()
        self._process.stdin.close()

        queue: asyncio.Queue[LogChunk | None] = asyncio.Queue()
        seq = 0

        async def read_stream(name: str, stream: asyncio.StreamReader | None) -> None:
            nonlocal seq
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    return
                seq += 1
                await queue.put(
                    LogChunk(
                        stream=cast(Literal["stdout", "stderr"], name),
                        seq=seq,
                        text=chunk.decode(errors="replace"),
                        ts_ms=int(time.time() * 1000),
                    )
                )

        stdout_task = asyncio.create_task(read_stream("stdout", self._process.stdout))
        stderr_task = asyncio.create_task(read_stream("stderr", self._process.stderr))
        wait_task = asyncio.create_task(self._process.wait())
        timed_out = False
        try:
            while True:
                if wait_task.done() and stdout_task.done() and stderr_task.done() and queue.empty():
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    if timeout_s > 0 and not timed_out and time.monotonic() - started > timeout_s:
                        timed_out = True
                        await self.cancel()
                    continue
                if chunk is not None:
                    yield chunk
            await asyncio.gather(stdout_task, stderr_task)
            exit_code = await wait_task
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
        yield ExecResult(
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
        )

    async def upload(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            source = Path(file.source)
            target = self._remote_path(file.target)
            size = source.stat().st_size
            await self._upload_file(source, target)
            yield TransferProgress(file=file, bytes_total=size, bytes_done=size)

    async def download(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            source = self._remote_path(file.source)
            target = Path(file.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = await self._download_file(source, target)
            yield TransferProgress(file=file, bytes_total=size, bytes_done=size)

    async def cancel(self) -> None:
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2)
            except TimeoutError:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                await self._process.wait()

    async def destroy(self) -> None:
        await self.cancel()

    async def _upload_file(self, source: Path, target: str) -> None:
        command = f"mkdir -p {shlex.quote(str(Path(target).parent))} && cat > {shlex.quote(target)}"
        process = await asyncio.create_subprocess_exec(
            *self._ssh_args(command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            with source.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    process.stdin.write(chunk)
                    await process.stdin.drain()
            process.stdin.close()
            await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
        if process.returncode != 0:
            raise RuntimeError(f"{self.host_label} upload failed: {stderr.strip()}")

    async def _download_file(self, source: str, target: Path) -> int:
        process = await asyncio.create_subprocess_exec(
            *self._ssh_args(f"cat {shlex.quote(source)}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        size = 0
        try:
            with target.open("wb") as handle:
                while chunk := await process.stdout.read(1024 * 1024):
                    handle.write(chunk)
                    size += len(chunk)
            await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
        if process.returncode != 0:
            raise RuntimeError(f"{self.host_label} download failed: {stderr.strip()}")
        return size

    async def _run_control_command(self, command: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *self._ssh_args(command),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"{self.host_label} remote command failed: {stderr.decode(errors='replace').strip()}")

    def _ssh_args(self, remote_command: str) -> list[str]:
        return ssh_args(
            self.ip,
            self.private_key_path,
            remote_command,
            user=self.ssh_user,
            port=self.ssh_port,
            connect_timeout_s=self.connect_timeout_s,
        )

    def _remote_path(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return f"{self.workspace.rstrip('/')}/{path}"


def ssh_args(
    ip: str,
    private_key: Path,
    remote_command: str,
    *,
    user: str = "root",
    port: int = 22,
    connect_timeout_s: int = 10,
) -> list[str]:
    return [
        "ssh",
        "-i",
        str(private_key),
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={connect_timeout_s}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        f"{user}@{ip}",
        remote_command,
    ]


def _remote_script(cwd: str, env: Mapping[str, str]) -> str:
    exports = []
    for key, value in env.items():
        if not _ENV_NAME.match(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        exports.append(f"export {key}={shlex.quote(value)}")
    lines = ["set -eu", f"cd {shlex.quote(cwd)}", *exports, 'exec "$@"']
    return "\n".join(lines) + "\n"


__all__ = ["SshExecSandbox", "ssh_args"]
