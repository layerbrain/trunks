from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol, Self


Isolation = Literal["process", "container", "microvm", "vm"]
NetworkMode = Literal["default", "none", "egress-only"]
Arch = Literal["x86_64", "arm64"]


@dataclass(frozen=True)
class GPU:
    kind: str
    count: int

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("gpu kind is required")
        if self.count < 1:
            raise ValueError("gpu count must be >= 1")


@dataclass(frozen=True)
class Spec:
    cpu: int
    memory_gib: int
    disk_gib: int
    arch: Arch = "x86_64"
    gpu: GPU | None = None
    network: NetworkMode = "default"

    def __post_init__(self) -> None:
        if self.cpu < 1:
            raise ValueError("cpu must be >= 1")
        if self.memory_gib < 1:
            raise ValueError("memory_gib must be >= 1")
        if self.disk_gib < 1:
            raise ValueError("disk_gib must be >= 1")
        if self.arch not in {"x86_64", "arm64"}:
            raise ValueError("arch must be x86_64 or arm64")
        if self.network not in {"default", "none", "egress-only"}:
            raise ValueError("network must be default, none, or egress-only")

    @property
    def key(self) -> str:
        gpu = "nogpu" if self.gpu is None else f"gpu{self.gpu.kind}x{self.gpu.count}"
        return f"cpu{self.cpu}-mem{self.memory_gib}g-disk{self.disk_gib}g-{self.arch}-{gpu}-{self.network}"


@dataclass(frozen=True)
class SandboxProviderInfo:
    id: str
    capabilities: frozenset[str]
    isolation: frozenset[Isolation]
    architectures: frozenset[Arch]
    network_modes: frozenset[NetworkMode]
    gpu_kinds: frozenset[str]
    max_cpu: int
    max_memory_gib: int
    max_disk_gib: int
    max_timeout_s: int
    specs: tuple[Spec, ...]
    regions: frozenset[str]
    secrets_passthrough: bool
    closure_fetch: bool
    artifact_upload: bool
    live_logs: bool

    def supports_static(
        self,
        *,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: Isolation | None,
        network: NetworkMode,
    ) -> bool:
        if not capabilities.issubset(self.capabilities):
            return False
        if isolation is not None and isolation not in self.isolation:
            return False
        if spec.arch not in self.architectures:
            return False
        if network not in self.network_modes:
            return False
        if region is not None and region not in self.regions:
            return False
        if spec.cpu > self.max_cpu or spec.memory_gib > self.max_memory_gib or spec.disk_gib > self.max_disk_gib:
            return False
        if spec.gpu is not None and spec.gpu.kind not in self.gpu_kinds:
            return False
        return True


@dataclass(frozen=True)
class SandboxRequest:
    run: str
    commit: str
    spec: Spec
    region: str | None
    isolation: Isolation
    network: NetworkMode
    timeout_s: int
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class SandboxFile:
    source: str
    target: str


@dataclass(frozen=True)
class HydrateProgress:
    bytes_total: int
    bytes_done: int
    objects_total: int
    objects_done: int


@dataclass(frozen=True)
class TransferProgress:
    file: SandboxFile
    bytes_total: int
    bytes_done: int


@dataclass(frozen=True)
class LogChunk:
    stream: Literal["stdout", "stderr"]
    seq: int
    text: str
    ts_ms: int


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    duration_ms: int
    timed_out: bool


@dataclass(frozen=True)
class ArtifactEntry:
    path: str
    size: int
    oid: str


@dataclass(frozen=True)
class ArtifactTree:
    entries: tuple[ArtifactEntry, ...]


Event = LogChunk | ExecResult


class SandboxProvider(Protocol):
    info: ClassVar[SandboxProviderInfo]

    @classmethod
    async def discover(cls, config: dict[str, object]) -> Self: ...

    async def supports(
        self,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: Isolation | None = None,
        network: NetworkMode = "default",
    ) -> bool: ...

    async def create(self, request: SandboxRequest) -> "Sandbox": ...


class Sandbox(Protocol):
    async def hydrate(self) -> AsyncIterator[HydrateProgress]: ...

    async def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_s: int,
    ) -> AsyncIterator[Event]: ...

    async def upload(
        self,
        files: tuple[SandboxFile, ...],
    ) -> AsyncIterator[TransferProgress]: ...

    async def download(
        self,
        files: tuple[SandboxFile, ...],
    ) -> AsyncIterator[TransferProgress]: ...

    async def cancel(self) -> None: ...

    async def destroy(self) -> None: ...
