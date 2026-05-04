from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
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


DEFAULT_API_URL = "https://app.daytona.io/api"
DEFAULT_TOOLBOX_URL = "https://proxy.app.daytona.io/toolbox"
DEFAULT_WORKSPACE = "/workspace"


@dataclass(frozen=True)
class _DaytonaConfig:
    api_key: str
    api_url: str
    toolbox_url: str
    organization_id: str | None
    region: str | None
    snapshot: str | None


@dataclass(frozen=True)
class _ExecutionResponse:
    stdout: str
    stderr: str
    exit_code: int


class DaytonaProvider:
    def __init__(self, config: _DaytonaConfig, client: "_DaytonaClient | None" = None) -> None:
        self.config = config
        self.client = client or _DaytonaClient(
            api_key=config.api_key,
            api_url=config.api_url,
            toolbox_url=config.toolbox_url,
            organization_id=config.organization_id,
        )
        regions = frozenset({config.region}) if config.region else frozenset({"global"})
        self.info = SandboxProviderInfo(
            id="daytona",
            capabilities=frozenset({"linux", "container", "remote"}),
            isolation=frozenset({"container"}),
            architectures=frozenset({"x86_64"}),
            network_modes=frozenset({"default", "egress-only"}),
            gpu_kinds=frozenset(),
            max_cpu=2,
            max_memory_gib=4,
            max_disk_gib=8,
            max_timeout_s=24 * 60 * 60,
            specs=(Spec(cpu=2, memory_gib=4, disk_gib=8),),
            regions=regions,
            secrets_passthrough=True,
            closure_fetch=True,
            artifact_upload=True,
            live_logs=False,
        )

    @classmethod
    async def discover(cls, config: dict[str, object]) -> "DaytonaProvider":
        parsed = _config(config)
        if parsed.api_key == "":
            raise LookupError("TRUNKS_DAYTONA_API_KEY or DAYTONA_API_KEY is not configured")
        return cls(parsed)

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

    async def doctor(self) -> dict[str, object]:
        return {
            "api_url": self.config.api_url,
            "toolbox_url": self.config.toolbox_url,
            "region": self.config.region or "global",
            "snapshot": self.config.snapshot,
            "catalog_source": "bundled",
            "catalog_size": len(self.info.specs),
        }

    async def create(self, request: SandboxRequest) -> "DaytonaSandbox":
        sandbox_id = await asyncio.to_thread(self.client.create_sandbox, request, snapshot=self.config.snapshot)
        return DaytonaSandbox(request, client=self.client, sandbox_id=sandbox_id)

    async def list_orphans(self, *, prefix: str = "trunks-") -> list[dict[str, object]]:
        sandboxes = await asyncio.to_thread(self.client.list_sandboxes)
        return [
            item
            for item in sandboxes
            if isinstance(item.get("id"), str)
            and isinstance(item.get("name"), str)
            and str(item["name"]).startswith(prefix)
        ]

    async def cleanup_orphans(self, *, prefix: str = "trunks-", dry_run: bool = True) -> dict[str, object]:
        orphans = await self.list_orphans(prefix=prefix)
        deleted: list[str] = []
        if not dry_run:
            for item in orphans:
                sandbox_id = str(item["id"])
                await asyncio.to_thread(self.client.delete_sandbox, sandbox_id)
                deleted.append(sandbox_id)
        return {
            "_schema_version": "trunks.sandboxes.provider_cleanup.v1",
            "object": "provider_cleanup",
            "provider": self.info.id,
            "prefix": prefix,
            "dry_run": dry_run,
            "orphans": orphans,
            "deleted": deleted,
        }


class DaytonaSandbox(Sandbox):
    def __init__(self, request: SandboxRequest, *, client: "_DaytonaClient", sandbox_id: str) -> None:
        self.request = request
        self.client = client
        self.sandbox_id = sandbox_id

    async def hydrate(self) -> AsyncIterator[HydrateProgress]:
        yield HydrateProgress(bytes_total=0, bytes_done=0, objects_total=0, objects_done=0)

    async def workspace_root(self) -> str:
        return await asyncio.to_thread(self.client.workspace_root, self.sandbox_id)

    async def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_s: int,
    ) -> AsyncIterator[Event]:
        started = time.monotonic()
        command_text = " ".join(shlex.quote(part) for part in command)
        response = await asyncio.to_thread(
            self.client.execute,
            self.sandbox_id,
            command_text,
            cwd=cwd,
            env=dict(env),
            timeout_s=timeout_s,
        )
        seq = 0
        if response.stdout:
            seq += 1
            yield LogChunk(stream="stdout", seq=seq, text=response.stdout, ts_ms=int(time.time() * 1000))
        if response.stderr:
            seq += 1
            yield LogChunk(stream="stderr", seq=seq, text=response.stderr, ts_ms=int(time.time() * 1000))
        yield ExecResult(
            exit_code=response.exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timeout_s > 0 and time.monotonic() - started > timeout_s,
        )

    async def upload(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            data = await asyncio.to_thread(Path(file.source).read_bytes)
            await asyncio.to_thread(self.client.upload, self.sandbox_id, file, data)
            yield TransferProgress(file=file, bytes_total=len(data), bytes_done=len(data))

    async def download(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            data = await asyncio.to_thread(self.client.download, self.sandbox_id, file)
            target = Path(file.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, data)
            yield TransferProgress(file=file, bytes_total=len(data), bytes_done=len(data))

    async def cancel(self) -> None:
        return None

    async def destroy(self) -> None:
        await asyncio.to_thread(self.client.delete_sandbox, self.sandbox_id)


class _DaytonaClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        toolbox_url: str = DEFAULT_TOOLBOX_URL,
        organization_id: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.toolbox_url = toolbox_url.rstrip("/")
        self.organization_id = organization_id
        self.created_directories: set[tuple[str, str]] = set()

    def create_sandbox(self, request: SandboxRequest, *, snapshot: str | None) -> str:
        body = {
            "name": _resource_name(request.run),
            "target": request.region if request.region and request.region != "global" else None,
            "snapshot": snapshot,
            "autoStopInterval": 5,
            "autoDeleteInterval": 0,
            "labels": {
                "trunks_run_id": request.run,
                "trunks_provider": "daytona",
            },
        }
        response = self._json_request("POST", self._api("/sandbox"), body=_strip_none(body))
        sandbox_id = response.get("id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise RuntimeError("Daytona create sandbox response did not include id")
        return sandbox_id

    def list_sandboxes(self) -> list[dict[str, object]]:
        response = self._json_request("GET", self._api("/sandbox"))
        return _items(response)

    def delete_sandbox(self, sandbox_id: str) -> None:
        self._json_request("DELETE", self._api(f"/sandbox/{urllib.parse.quote(sandbox_id, safe='')}"), allow_404=True)

    def workspace_root(self, sandbox_id: str) -> str:
        response = self._json_request("GET", self._toolbox(sandbox_id, "/work-dir"))
        root = str(response.get("dir") or response.get("path") or DEFAULT_WORKSPACE)
        self.created_directories.add((sandbox_id, root))
        return root

    def execute(
        self,
        sandbox_id: str,
        command: str,
        *,
        cwd: str,
        env: dict[str, str],
        timeout_s: int,
    ) -> _ExecutionResponse:
        response = self._json_request(
            "POST",
            self._toolbox(sandbox_id, "/process/execute"),
            body={"command": command, "cwd": cwd, "timeout": timeout_s, "envs": env},
        )
        result = response.get("result")
        if isinstance(result, str):
            return _ExecutionResponse(
                stdout=result,
                stderr=str(response.get("stderr") or response.get("error") or ""),
                exit_code=int(response.get("exitCode") or response.get("exit_code") or response.get("code") or 0),
            )
        if not isinstance(result, dict):
            result = response
        return _ExecutionResponse(
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or response.get("stderr") or response.get("error") or ""),
            exit_code=int(result.get("exitCode") or result.get("exit_code") or result.get("code") or 0),
        )

    def upload(self, sandbox_id: str, file: SandboxFile, data: bytes) -> None:
        self.ensure_directory(sandbox_id, str(Path(file.target).parent))
        boundary = f"----trunks-{uuid.uuid4().hex}"
        filename = Path(file.source).name
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        url = self._toolbox(sandbox_id, "/files/upload", query={"path": file.target})
        self._raw_request("POST", url, body=body, content_type=f"multipart/form-data; boundary={boundary}")

    def download(self, sandbox_id: str, file: SandboxFile) -> bytes:
        return self._raw_request("GET", self._toolbox(sandbox_id, "/files/download", query={"path": file.source}))

    def ensure_directory(self, sandbox_id: str, path: str) -> None:
        if not path or path == ".":
            return
        key = (sandbox_id, path)
        if key in self.created_directories:
            return
        self._json_request("POST", self._toolbox(sandbox_id, "/files/folder", query={"path": path, "mode": "0755"}))
        self.created_directories.add(key)

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        content_type: str | None,
        headers: dict[str, str],
    ) -> bytes:
        data = body
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode(errors="replace")
            raise RuntimeError(f"Daytona API {method} {url} failed: {exc.code} {details}") from exc

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, object] | None = None,
        allow_404: bool = False,
    ) -> dict[str, object]:
        try:
            raw = self._raw_request(
                method,
                url,
                body=None if body is None else json.dumps(body).encode(),
                content_type="application/json" if body is not None else None,
            )
        except RuntimeError as exc:
            if allow_404 and "failed: 404 " in str(exc):
                return {}
            raise
        if not raw:
            return {}
        parsed = json.loads(raw.decode())
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def _raw_request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if self.organization_id:
            headers["X-Daytona-Organization-ID"] = self.organization_id
        if content_type is not None:
            headers["Content-Type"] = content_type
        return self.request(method, url, body=body, content_type=content_type, headers=headers)

    def _api(self, path: str) -> str:
        return f"{self.api_url}{path}"

    def _toolbox(self, sandbox_id: str, path: str, query: dict[str, str] | None = None) -> str:
        quoted_id = urllib.parse.quote(sandbox_id, safe="")
        url = f"{self.toolbox_url}/{quoted_id}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url


def _config(config: dict[str, object]) -> _DaytonaConfig:
    return _DaytonaConfig(
        api_key=_config_str(config, "api_key") or os.environ.get("TRUNKS_DAYTONA_API_KEY") or os.environ.get("DAYTONA_API_KEY") or "",
        api_url=_config_str(config, "api_url") or os.environ.get("DAYTONA_API_URL") or DEFAULT_API_URL,
        toolbox_url=_config_str(config, "toolbox_url") or os.environ.get("DAYTONA_TOOLBOX_URL") or DEFAULT_TOOLBOX_URL,
        organization_id=_config_str(config, "organization_id") or os.environ.get("DAYTONA_ORGANIZATION_ID"),
        region=_config_str(config, "region") or os.environ.get("DAYTONA_REGION"),
        snapshot=_config_str(config, "snapshot") or os.environ.get("DAYTONA_SNAPSHOT"),
    )


def _config_str(config: dict[str, object], key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def _items(response: dict[str, object]) -> list[dict[str, object]]:
    for key in ("sandboxes", "data", "items"):
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _resource_name(run: str) -> str:
    safe = "".join(char if char.isalnum() or char == "-" else "-" for char in run.lower()).strip("-")
    return f"trunks-{(safe or uuid.uuid4().hex)[:18]}-{uuid.uuid4().hex[:8]}"


def _strip_none(value: dict[str, object | None]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item is not None}


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_TOOLBOX_URL",
    "DEFAULT_WORKSPACE",
    "DaytonaProvider",
    "DaytonaSandbox",
    "_DaytonaClient",
    "_DaytonaConfig",
]
