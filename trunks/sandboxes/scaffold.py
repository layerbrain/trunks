from __future__ import annotations

from pathlib import Path


def scaffold_provider(provider_id: str, *, output: str | Path) -> Path:
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    module = root / f"{provider_id.replace('-', '_')}.py"
    class_name = "".join(part.capitalize() for part in provider_id.replace("_", "-").split("-")) + "Provider"
    module.write_text(_provider_source(provider_id, class_name), encoding="utf-8")
    return module


def _provider_source(provider_id: str, class_name: str) -> str:
    return f'''from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from trunks.sandboxes import (
    Event,
    HydrateProgress,
    Isolation,
    NetworkMode,
    Sandbox,
    SandboxFile,
    SandboxProviderInfo,
    SandboxRequest,
    Spec,
    TransferProgress,
)


class {class_name}:
    info = SandboxProviderInfo(
        id="{provider_id}",
        capabilities=frozenset({{"linux"}}),
        isolation=frozenset({{"microvm"}}),
        architectures=frozenset({{"x86_64", "arm64"}}),
        network_modes=frozenset({{"default", "egress-only", "none"}}),
        gpu_kinds=frozenset(),
        max_cpu=8,
        max_memory_gib=32,
        max_disk_gib=100,
        max_timeout_s=60 * 60,
        specs=(Spec(cpu=1, memory_gib=1, disk_gib=10),),
        regions=frozenset({{"local"}}),
        secrets_passthrough=True,
        closure_fetch=True,
        artifact_upload=True,
        live_logs=True,
    )

    @classmethod
    async def discover(cls, config: dict[str, object]) -> "{class_name}":
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

    async def create(self, request: SandboxRequest) -> "ExampleSandbox":
        raise NotImplementedError("create a sandbox through your provider HTTP API")


class ExampleSandbox(Sandbox):
    async def hydrate(self) -> AsyncIterator[HydrateProgress]:
        yield HydrateProgress(bytes_total=0, bytes_done=0, objects_total=0, objects_done=0)

    async def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_s: int,
    ) -> AsyncIterator[Event]:
        raise NotImplementedError

    async def upload(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        raise NotImplementedError

    async def download(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        raise NotImplementedError

    async def cancel(self) -> None:
        raise NotImplementedError

    async def destroy(self) -> None:
        raise NotImplementedError
'''
