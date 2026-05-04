from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from ..provider import (
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


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


class DockerProvider:
    info = SandboxProviderInfo(
        id="docker",
        capabilities=frozenset({"docker", "linux", "container"}),
        isolation=frozenset({"container"}),
        architectures=frozenset({_host_arch()}),
        network_modes=frozenset({"default", "none"}),
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

    def __init__(self, *, image: str = "ubuntu:24.04") -> None:
        self.image = image

    @classmethod
    async def discover(cls, config: dict[str, object]) -> "DockerProvider":
        docker = shutil.which("docker")
        if docker is None:
            raise LookupError("docker CLI not found")
        process = await asyncio.create_subprocess_exec(
            docker,
            "info",
            "--format",
            "{{json .ServerVersion}}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() != 0:
            raise LookupError("docker daemon is not reachable")
        image = config.get("image")
        return cls(image=image if isinstance(image, str) and image else "ubuntu:24.04")

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

    async def create(self, request: SandboxRequest) -> "DockerSandbox":
        return DockerSandbox(request, image=self.image)


class DockerSandbox(Sandbox):
    def __init__(self, request: SandboxRequest, *, image: str) -> None:
        self.request = request
        self.image = image
        self._root = Path(tempfile.mkdtemp(prefix="trunks-docker-")).resolve()
        self._tmp = self._root / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._container = f"trunks-{request.run.lower()[:16]}-{os.getpid()}-{id(self):x}"
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
        docker_cmd, docker_env = self._docker_command(command, env, cwd)
        self._process = await asyncio.create_subprocess_exec(
            *docker_cmd,
            env=docker_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
                text = chunk.decode(errors="replace")
                if name == "stderr" and text.startswith("WARNING: The requested image's platform"):
                    continue
                seq += 1
                await queue.put(
                    LogChunk(
                        stream=name,  # type: ignore[arg-type]
                        seq=seq,
                        text=text,
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
        yield ExecResult(exit_code=exit_code, duration_ms=int((time.monotonic() - started) * 1000), timed_out=timed_out)

    async def upload(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            source = Path(file.source)
            target = self._sandbox_path(file.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = source.stat().st_size
            shutil.copyfile(source, target)
            yield TransferProgress(file=file, bytes_total=size, bytes_done=size)

    async def download(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            source = self._sandbox_path(file.source)
            target = Path(file.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = source.stat().st_size
            shutil.copyfile(source, target)
            yield TransferProgress(file=file, bytes_total=size, bytes_done=size)

    async def cancel(self) -> None:
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
        await self._remove_container()

    async def destroy(self) -> None:
        await self.cancel()
        shutil.rmtree(self._root, ignore_errors=True)

    def _docker_command(self, command: list[str], env: Mapping[str, str], cwd: str) -> tuple[list[str], dict[str, str]]:
        host_cwd = str(Path(cwd).resolve())
        docker_env = dict(os.environ)
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            self._container,
            "-v",
            f"{host_cwd}:{host_cwd}",
            "-v",
            f"{self._tmp}:/tmp",
            "-w",
            host_cwd,
            "--cpus",
            str(self.request.spec.cpu),
            "--memory",
            f"{self.request.spec.memory_gib}g",
        ]
        if self.request.network == "none":
            cmd.extend(["--network", "none"])
        for key, value in env.items():
            if not key or "=" in key:
                raise ValueError(f"invalid environment variable name: {key!r}")
            docker_env[key] = value
            cmd.extend(["--env", key])
        cmd.append(self.image)
        cmd.extend(command)
        return cmd, docker_env

    async def _remove_container(self) -> None:
        docker = shutil.which("docker")
        if docker is None:
            return
        process = await asyncio.create_subprocess_exec(
            docker,
            "rm",
            "-f",
            self._container,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()

    def _sandbox_path(self, path: str) -> Path:
        raw = Path(path)
        relative = raw.relative_to("/") if raw.is_absolute() else raw
        candidate = (self._root / relative).resolve(strict=False)
        if not candidate.is_relative_to(self._root):
            raise ValueError(f"sandbox path escapes container root: {path}")
        return candidate
