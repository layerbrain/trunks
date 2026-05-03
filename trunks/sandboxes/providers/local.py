from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from ..provider import (
    ArtifactEntry,
    ArtifactTree,
    Event,
    ExecResult,
    HydrateProgress,
    Isolation,
    LogChunk,
    NetworkMode,
    Sandbox,
    SandboxFile,
    SandboxProviderInfo,
    SandboxRequest,
    Spec,
    TransferProgress,
)


def _host_capability() -> str:
    if os.name == "nt":
        return "windows"
    if os.uname().sysname == "Darwin":
        return "macos"
    return "linux"


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


class LocalProvider:
    info = SandboxProviderInfo(
        id="local",
        capabilities=frozenset({"local", _host_capability()}),
        isolation=frozenset({"process"}),
        architectures=frozenset({_host_arch()}),
        network_modes=frozenset({"default"}),
        gpu_kinds=frozenset(),
        max_cpu=os.cpu_count() or 1,
        max_memory_gib=1024,
        max_disk_gib=1024,
        max_timeout_s=24 * 60 * 60,
        specs=(Spec(cpu=1, memory_gib=1, disk_gib=1, arch=_host_arch()),),
        regions=frozenset({"local"}),
        secrets_passthrough=True,
        closure_fetch=True,
        artifact_upload=True,
        live_logs=True,
    )

    @classmethod
    async def discover(cls, config: dict[str, object]) -> "LocalProvider":
        return cls()

    async def supports(
        self,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: Isolation | None = None,
        network: NetworkMode = "default",
    ) -> bool:
        return self.info.supports_static(
            capabilities=capabilities,
            spec=spec,
            region=region,
            isolation=isolation,
            network=network,
        )

    async def create(self, request: SandboxRequest) -> "LocalSandbox":
        return LocalSandbox(request)


class LocalSandbox(Sandbox):
    def __init__(self, request: SandboxRequest) -> None:
        self.request = request
        self._process: asyncio.subprocess.Process | None = None

    async def hydrate(self) -> AsyncIterator[HydrateProgress]:
        yield HydrateProgress(bytes_total=0, bytes_done=0, objects_total=0, objects_done=0)

    async def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_s: int,
    ) -> AsyncIterator[Event]:
        started = time.monotonic()
        process_env = dict(os.environ)
        process_env.update(env)
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        self._process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=process_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
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
                        stream=name,  # type: ignore[arg-type]
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
                        self._kill_process()
                    continue
                if chunk is not None:
                    yield chunk
            await asyncio.gather(stdout_task, stderr_task)
            exit_code = await wait_task
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
        yield ExecResult(exit_code=exit_code, duration_ms=int((time.monotonic() - started) * 1000), timed_out=timed_out)

    async def upload(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            source = Path(file.source)
            target = Path(file.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = source.stat().st_size
            shutil.copyfile(source, target)
            yield TransferProgress(file=file, bytes_total=size, bytes_done=size)

    async def download(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            source = Path(file.source)
            target = Path(file.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = source.stat().st_size
            shutil.copyfile(source, target)
            yield TransferProgress(file=file, bytes_total=size, bytes_done=size)

    async def artifact_tree(self, paths: tuple[str, ...]) -> ArtifactTree:
        entries: list[ArtifactEntry] = []
        for raw_path in paths:
            path = Path(raw_path)
            if path.is_file():
                entries.append(ArtifactEntry(path=str(path), size=path.stat().st_size, oid=""))
        return ArtifactTree(entries=tuple(entries))

    async def cancel(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._terminate_process()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.5)
            except TimeoutError:
                self._kill_process()
                await self._process.wait()

    async def destroy(self) -> None:
        await self.cancel()

    def _terminate_process(self) -> None:
        if self._process is None:
            return
        try:
            if os.name != "nt" and self._process.pid is not None:
                pgid = os.getpgid(self._process.pid)
                if pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    self._process.terminate()
            else:
                self._process.terminate()
        except ProcessLookupError:
            pass

    def _kill_process(self) -> None:
        if self._process is None:
            return
        try:
            if os.name != "nt" and self._process.pid is not None:
                pgid = os.getpgid(self._process.pid)
                if pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    self._process.kill()
            else:
                self._process.kill()
        except ProcessLookupError:
            pass
