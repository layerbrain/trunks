from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from ..provider import (
    GPU,
    Isolation,
    NetworkMode,
    SandboxProviderInfo,
    SandboxRequest,
    Spec,
)
from ..ssh_exec import SshExecSandbox


DEFAULT_API_URL = "https://api.primeintellect.ai"
DEFAULT_IMAGE = "ubuntu_22_cuda_12"
DEFAULT_WORKSPACE = "/root/trunks"
DEFAULT_KEY_PREFIX = "trunks-"
PROVIDER_TYPE = "primeintellect"

_GPU_KINDS: tuple[str, ...] = (
    "A10_24GB",
    "A100_40GB",
    "A100_80GB",
    "A2000_6GB",
    "A30_24GB",
    "A40_48GB",
    "A4000_16GB",
    "A4500_20GB",
    "A5000_24GB",
    "A6000_48GB",
    "B200_180GB",
    "B300_262GB",
    "GH200_96GB",
    "GH200_480GB",
    "GH200_624GB",
    "H100_80GB",
    "H200_96GB",
    "H200_141GB",
    "L4_24GB",
    "L40_48GB",
    "L40S_48GB",
    "P4_8GB",
    "P40_24GB",
    "P100_16GB",
    "RTX2000Ada_16GB",
    "RTX3070_8GB",
    "RTX3080_10GB",
    "RTX3080Ti_12GB",
    "RTX3090_24GB",
    "RTX3090Ti_24GB",
    "RTX4000_8GB",
    "RTX4000Ada_20GB",
    "RTX4070Ti_12GB",
    "RTX4080_16GB",
    "RTX4080Ti_16GB",
    "RTX4090_24GB",
    "RTX5000_16GB",
    "RTX5000Ada_32GB",
    "RTX5090_32GB",
    "RTX6000_24GB",
    "RTX6000Ada_48GB",
    "RTX8000_48GB",
    "RTX_PRO_6000B_96GB",
    "T4_16GB",
    "V100_16GB",
    "V100_32GB",
)

_REGIONS: tuple[str, ...] = ("us", "india", "europe", "asia", "global")

_SOCKETS: tuple[str, ...] = ("PCIe", "SXM2", "SXM3", "SXM4", "SXM5", "SXM6")

_RESOURCE_NAME = re.compile(r"[^a-z0-9-]+")
_SSH_INVOCATION = re.compile(r"ssh\s+(?P<args>.+)", re.IGNORECASE)


_SPEC_CATALOG: tuple[Spec, ...] = (
    Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H100_80GB", count=1)),
    Spec(cpu=16, memory_gib=64, disk_gib=200, gpu=GPU(kind="H100_80GB", count=2)),
    Spec(cpu=16, memory_gib=64, disk_gib=400, gpu=GPU(kind="H100_80GB", count=4)),
    Spec(cpu=16, memory_gib=64, disk_gib=800, gpu=GPU(kind="H100_80GB", count=8)),
    Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H200_141GB", count=1)),
    Spec(cpu=16, memory_gib=64, disk_gib=400, gpu=GPU(kind="H200_141GB", count=4)),
    Spec(cpu=16, memory_gib=64, disk_gib=800, gpu=GPU(kind="H200_141GB", count=8)),
    Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="A100_80GB", count=1)),
    Spec(cpu=16, memory_gib=64, disk_gib=200, gpu=GPU(kind="A100_80GB", count=2)),
    Spec(cpu=16, memory_gib=64, disk_gib=400, gpu=GPU(kind="A100_80GB", count=4)),
    Spec(cpu=16, memory_gib=64, disk_gib=800, gpu=GPU(kind="A100_80GB", count=8)),
    Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="A100_40GB", count=1)),
    Spec(cpu=16, memory_gib=64, disk_gib=200, gpu=GPU(kind="A100_40GB", count=2)),
    Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="B200_180GB", count=1)),
    Spec(cpu=16, memory_gib=64, disk_gib=400, gpu=GPU(kind="B200_180GB", count=4)),
    Spec(cpu=16, memory_gib=64, disk_gib=800, gpu=GPU(kind="B200_180GB", count=8)),
    Spec(cpu=4, memory_gib=16, disk_gib=80, gpu=GPU(kind="A40_48GB", count=1)),
    Spec(cpu=8, memory_gib=32, disk_gib=160, gpu=GPU(kind="A40_48GB", count=2)),
    Spec(cpu=4, memory_gib=16, disk_gib=80, gpu=GPU(kind="L40S_48GB", count=1)),
    Spec(cpu=8, memory_gib=32, disk_gib=160, gpu=GPU(kind="L40S_48GB", count=2)),
    Spec(cpu=4, memory_gib=16, disk_gib=80, gpu=GPU(kind="L40_48GB", count=1)),
    Spec(cpu=4, memory_gib=8, disk_gib=60, gpu=GPU(kind="L4_24GB", count=1)),
    Spec(cpu=4, memory_gib=16, disk_gib=80, gpu=GPU(kind="A6000_48GB", count=1)),
    Spec(cpu=8, memory_gib=32, disk_gib=160, gpu=GPU(kind="A6000_48GB", count=2)),
    Spec(cpu=4, memory_gib=16, disk_gib=60, gpu=GPU(kind="RTX4090_24GB", count=1)),
    Spec(cpu=8, memory_gib=32, disk_gib=120, gpu=GPU(kind="RTX4090_24GB", count=2)),
    Spec(cpu=4, memory_gib=16, disk_gib=60, gpu=GPU(kind="RTX5090_32GB", count=1)),
    Spec(cpu=4, memory_gib=16, disk_gib=80, gpu=GPU(kind="RTX_PRO_6000B_96GB", count=1)),
)


@dataclass(frozen=True)
class _PrimeIntellectConfig:
    api_key: str
    api_url: str
    region: str | None
    data_center_id: str | None
    country: str | None
    image: str
    workspace: str
    allowed_specs: frozenset[str]
    boot_timeout_s: int
    ssh_timeout_s: int


@dataclass(frozen=True)
class _SSHKey:
    id: str
    name: str


@dataclass(frozen=True)
class _Pod:
    id: str
    name: str


@dataclass(frozen=True)
class _PodInfo:
    id: str
    status: str
    ssh_user: str
    ssh_host: str
    ssh_port: int


class PrimeIntellectProvider:
    def __init__(
        self,
        config: _PrimeIntellectConfig,
        client: "_PrimeIntellectClient | None" = None,
    ) -> None:
        self.config = config
        self.client = client or _PrimeIntellectClient(config.api_key, config.api_url)
        specs = _filter_specs(_SPEC_CATALOG, config.allowed_specs)
        if not specs:
            raise LookupError("PRIMEINTELLECT_ALLOWED_SPECS filtered out every available spec")
        max_cpu = max(spec.cpu for spec in specs)
        max_memory = max(spec.memory_gib for spec in specs)
        max_disk = max(spec.disk_gib for spec in specs)
        gpu_kinds = frozenset(spec.gpu.kind for spec in specs if spec.gpu is not None)
        if config.region is None:
            regions = frozenset(_REGIONS)
        else:
            regions = frozenset({config.region})
        self.info = SandboxProviderInfo(
            id="primeintellect",
            capabilities=frozenset({"linux", "remote", "vm", "gpu"}),
            isolation=frozenset({"vm"}),
            architectures=frozenset({"x86_64"}),
            network_modes=frozenset({"default"}),
            gpu_kinds=gpu_kinds,
            max_cpu=max_cpu,
            max_memory_gib=max_memory,
            max_disk_gib=max_disk,
            max_timeout_s=24 * 60 * 60,
            specs=specs,
            regions=regions,
            secrets_passthrough=True,
            closure_fetch=True,
            artifact_upload=True,
            live_logs=True,
        )

    @classmethod
    async def discover(cls, config: dict[str, object]) -> "PrimeIntellectProvider":
        if shutil.which("ssh") is None:
            raise LookupError("ssh CLI not found")
        if shutil.which("ssh-keygen") is None:
            raise LookupError("ssh-keygen CLI not found")
        parsed = _config(config)
        if parsed.api_key == "":
            raise LookupError("TRUNKS_PRIMEINTELLECT_API_KEY is not configured")
        return cls(parsed)

    async def supports(
        self,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: Isolation | None = None,
        network: NetworkMode = "default",
    ) -> bool:
        if spec.gpu is None:
            return False
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
            "region": self.config.region,
            "data_center_id": self.config.data_center_id,
            "country": self.config.country,
            "image": self.config.image,
            "workspace": self.config.workspace,
            "ssh": "available",
            "catalog_source": "bundled",
            "catalog_size": len(self.info.specs),
        }

    async def create(self, request: SandboxRequest) -> "PrimeIntellectSandbox":
        if request.spec.gpu is None:
            raise RuntimeError("primeintellect requires a GPU spec")
        name = _resource_name(request.run)
        key_dir = Path(tempfile.mkdtemp(prefix="trunks-prime-key-"))
        private_key = key_dir / "id_ed25519"
        public_key = key_dir / "id_ed25519.pub"
        ssh_key: _SSHKey | None = None
        pod: _Pod | None = None
        try:
            await _generate_keypair(private_key)
            ssh_key = await asyncio.to_thread(
                self.client.upload_ssh_key,
                name,
                public_key.read_text(encoding="utf-8"),
            )
            availability = await asyncio.to_thread(
                self.client.find_availability,
                gpu_type=request.spec.gpu.kind,
                gpu_count=request.spec.gpu.count,
                region=request.region or self.config.region,
                data_center_id=self.config.data_center_id,
            )
            pod = await asyncio.to_thread(
                self.client.create_pod,
                name=name,
                availability=availability,
                spec=request.spec,
                run=request.run,
                ssh_key_id=ssh_key.id,
                image=self.config.image,
                country=self.config.country,
            )
            connection = await _wait_for_pod_ssh(self.client, pod.id, timeout_s=self.config.boot_timeout_s)
            await _wait_for_ssh(connection, private_key, timeout_s=self.config.ssh_timeout_s)
            return PrimeIntellectSandbox(
                request,
                client=self.client,
                pod=pod,
                ssh_key=ssh_key,
                private_key=private_key,
                key_dir=key_dir,
                ssh_user=connection.ssh_user,
                ssh_host=connection.ssh_host,
                ssh_port=connection.ssh_port,
                workspace=self.config.workspace,
            )
        except Exception:
            if pod is not None:
                await asyncio.to_thread(self.client.delete_pod, pod.id)
            if ssh_key is not None:
                await asyncio.to_thread(self.client.delete_ssh_key, ssh_key.id)
            shutil.rmtree(key_dir, ignore_errors=True)
            raise

    async def list_orphans(self, *, prefix: str = DEFAULT_KEY_PREFIX) -> list[dict[str, object]]:
        pods, keys = await asyncio.gather(
            asyncio.to_thread(self.client.list_pods),
            asyncio.to_thread(self.client.list_ssh_keys),
        )
        orphans: list[dict[str, object]] = []
        for pod in pods:
            name = str(pod.get("name") or "")
            pod_id = pod.get("id")
            if isinstance(pod_id, str) and name.startswith(prefix):
                orphans.append(
                    {
                        "type": "pod",
                        "id": pod_id,
                        "name": name,
                        "status": pod.get("status"),
                    }
                )
        for key in keys:
            name = str(key.get("name") or "")
            key_id = key.get("id")
            if isinstance(key_id, str) and name.startswith(prefix):
                orphans.append({"type": "ssh_key", "id": key_id, "name": name})
        return orphans

    async def cleanup_orphans(
        self, *, prefix: str = DEFAULT_KEY_PREFIX, dry_run: bool = True
    ) -> dict[str, object]:
        orphans = await self.list_orphans(prefix=prefix)
        deleted: list[dict[str, object]] = []
        if not dry_run:
            for item in orphans:
                resource_type = item.get("type")
                resource_id = item.get("id")
                if not isinstance(resource_id, str):
                    continue
                if resource_type == "pod":
                    await asyncio.to_thread(self.client.delete_pod, resource_id)
                elif resource_type == "ssh_key":
                    await asyncio.to_thread(self.client.delete_ssh_key, resource_id)
                else:
                    continue
                deleted.append({"type": str(resource_type), "id": resource_id})
        return {
            "_schema_version": "trunks.sandboxes.provider_cleanup.v1",
            "object": "provider_cleanup",
            "provider": self.info.id,
            "prefix": prefix,
            "dry_run": dry_run,
            "orphans": orphans,
            "deleted": deleted,
        }


class PrimeIntellectSandbox(SshExecSandbox):
    def __init__(
        self,
        request: SandboxRequest,
        *,
        client: "_PrimeIntellectClient",
        pod: _Pod,
        ssh_key: _SSHKey,
        private_key: Path,
        key_dir: Path,
        ssh_user: str,
        ssh_host: str,
        ssh_port: int,
        workspace: str,
    ) -> None:
        super().__init__(
            request,
            ip=ssh_host,
            private_key_path=private_key,
            workspace=workspace,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            host_label="PrimeIntellect",
        )
        self.client = client
        self.pod = pod
        self.ssh_key = ssh_key
        self.private_key = private_key
        self.key_dir = key_dir

    async def destroy(self) -> None:
        await self.cancel()
        await asyncio.gather(
            asyncio.to_thread(self.client.delete_pod, self.pod.id),
            asyncio.to_thread(self.client.delete_ssh_key, self.ssh_key.id),
        )
        shutil.rmtree(self.key_dir, ignore_errors=True)


class _PrimeIntellectClient:
    def __init__(self, api_key: str, api_url: str = DEFAULT_API_URL) -> None:
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")

    def upload_ssh_key(self, name: str, public_key: str) -> _SSHKey:
        response = self._request(
            "POST",
            "/api/v1/ssh_keys/",
            body={"name": name, "publicKey": public_key.strip()},
        )
        key_id = response.get("id")
        if not isinstance(key_id, str) or not key_id:
            raise RuntimeError("Prime Intellect upload SSH key response did not include id")
        return _SSHKey(id=key_id, name=str(response.get("name") or name))

    def delete_ssh_key(self, key_id: str) -> None:
        self._request("DELETE", f"/api/v1/ssh_keys/{key_id}", allow_404=True)

    def list_ssh_keys(self) -> list[dict[str, object]]:
        response = self._request("GET", "/api/v1/ssh_keys/")
        return _items(response)

    def list_pods(self) -> list[dict[str, object]]:
        response = self._request("GET", "/api/v1/pods/")
        return _items(response)

    def find_availability(
        self,
        *,
        gpu_type: str,
        gpu_count: int,
        region: str | None,
        data_center_id: str | None,
    ) -> dict[str, object]:
        query: dict[str, str] = {
            "gpu_type": gpu_type,
            "gpu_count": str(gpu_count),
            "page_size": "100",
        }
        if region:
            query["regions"] = region
        if data_center_id:
            query["data_center_id"] = data_center_id
        response = self._request("GET", "/api/v1/availability/gpus", query=query)
        items = response.get("items")
        if not isinstance(items, list) or not items:
            raise RuntimeError(
                f"Prime Intellect availability returned no GPU offers for "
                f"{gpu_type} x{gpu_count} in region {region or 'any'}"
            )
        for offer in items:
            if isinstance(offer, dict) and str(offer.get("provider")) == PROVIDER_TYPE:
                return offer
        offer = items[0]
        if not isinstance(offer, dict):
            raise RuntimeError("Prime Intellect availability returned a non-object offer")
        return offer

    def create_pod(
        self,
        *,
        name: str,
        availability: dict[str, object],
        spec: Spec,
        run: str,
        ssh_key_id: str,
        image: str,
        country: str | None,
    ) -> _Pod:
        cloud_id = availability.get("cloudId")
        gpu_type = availability.get("gpuType")
        socket = availability.get("socket")
        if not isinstance(cloud_id, str) or not isinstance(gpu_type, str) or not isinstance(socket, str):
            raise RuntimeError(
                "Prime Intellect availability offer missing cloudId/gpuType/socket"
            )
        provider_type = availability.get("provider")
        provider_value = provider_type if isinstance(provider_type, str) else PROVIDER_TYPE
        pod_body: dict[str, object] = {
            "name": name,
            "cloudId": cloud_id,
            "gpuType": gpu_type,
            "socket": socket,
            "gpuCount": spec.gpu.count if spec.gpu is not None else 1,
            "vcpus": spec.cpu,
            "memory": spec.memory_gib,
            "diskSize": spec.disk_gib,
            "image": image,
            "sshKeyId": ssh_key_id,
            "envVars": [{"key": "TRUNKS_RUN_ID", "value": run}],
        }
        data_center_id = availability.get("dataCenterId") or availability.get("dataCenter")
        if isinstance(data_center_id, str) and data_center_id:
            pod_body["dataCenterId"] = data_center_id
        if country is not None:
            pod_body["country"] = country
        elif isinstance(availability.get("country"), str):
            pod_body["country"] = availability["country"]
        body = {"pod": pod_body, "provider": {"type": provider_value}}
        response = self._request("POST", "/api/v1/pods/", body=body)
        pod_id = response.get("id")
        if not isinstance(pod_id, str) or not pod_id:
            raise RuntimeError("Prime Intellect create pod response did not include id")
        return _Pod(id=pod_id, name=str(response.get("name") or name))

    def get_pod(self, pod_id: str) -> dict[str, object]:
        return self._request("GET", f"/api/v1/pods/{pod_id}")

    def delete_pod(self, pod_id: str) -> None:
        self._request("DELETE", f"/api/v1/pods/{pod_id}", allow_404=True)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
        allow_404: bool = False,
    ) -> dict[str, object]:
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode(errors="replace")
            if allow_404 and exc.code == 404:
                return {}
            raise RuntimeError(
                f"Prime Intellect API {method} {path} failed: {exc.code} {details}"
            ) from exc
        if not raw:
            return {}
        parsed = json.loads(raw.decode())
        return parsed if isinstance(parsed, dict) else {"data": parsed}


def _config(config: dict[str, object]) -> _PrimeIntellectConfig:
    api_key = (
        _config_str(config, "api_key")
        or os.environ.get("TRUNKS_PRIMEINTELLECT_API_KEY")
        or ""
    )
    api_url = (
        _config_str(config, "api_url")
        or os.environ.get("PRIMEINTELLECT_API_URL")
        or DEFAULT_API_URL
    )
    region = (
        _config_str(config, "region")
        or os.environ.get("PRIMEINTELLECT_REGION")
    )
    data_center_id = (
        _config_str(config, "data_center_id")
        or os.environ.get("PRIMEINTELLECT_DATACENTER")
    )
    country = (
        _config_str(config, "country")
        or os.environ.get("PRIMEINTELLECT_COUNTRY")
    )
    image = (
        _config_str(config, "image")
        or os.environ.get("PRIMEINTELLECT_IMAGE")
        or DEFAULT_IMAGE
    )
    workspace = (
        _config_str(config, "workspace")
        or os.environ.get("PRIMEINTELLECT_WORKSPACE")
        or DEFAULT_WORKSPACE
    )
    allowed_raw = (
        _config_str(config, "allowed_specs")
        or os.environ.get("PRIMEINTELLECT_ALLOWED_SPECS")
        or ""
    )
    allowed_specs = frozenset(
        item.strip() for item in allowed_raw.split(",") if item.strip()
    )
    boot_timeout_s = int(
        config.get("boot_timeout_s")
        or os.environ.get("PRIMEINTELLECT_BOOT_TIMEOUT_S")
        or 900
    )
    ssh_timeout_s = int(
        config.get("ssh_timeout_s")
        or os.environ.get("PRIMEINTELLECT_SSH_TIMEOUT_S")
        or 600
    )
    return _PrimeIntellectConfig(
        api_key=api_key,
        api_url=api_url,
        region=region,
        data_center_id=data_center_id,
        country=country,
        image=image,
        workspace=workspace,
        allowed_specs=allowed_specs,
        boot_timeout_s=boot_timeout_s,
        ssh_timeout_s=ssh_timeout_s,
    )


def _config_str(config: dict[str, object], key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def _filter_specs(catalog: tuple[Spec, ...], allowed: frozenset[str]) -> tuple[Spec, ...]:
    if not allowed:
        return catalog
    return tuple(spec for spec in catalog if spec.key in allowed or (spec.gpu is not None and spec.gpu.kind in allowed))


def _items(response: dict[str, object]) -> list[dict[str, object]]:
    for key in ("items", "data", "pods", "sshKeys", "ssh_keys"):
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _resource_name(run: str) -> str:
    raw = _RESOURCE_NAME.sub("-", run.lower()).strip("-") or uuid.uuid4().hex[:12]
    return f"{DEFAULT_KEY_PREFIX}{raw[:26]}-{uuid.uuid4().hex[:8]}"


async def _generate_keypair(private_key: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "ssh-keygen",
        "-t",
        "ed25519",
        "-N",
        "",
        "-f",
        str(private_key),
        "-C",
        "trunks",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {stderr.decode(errors='replace').strip()}")
    private_key.chmod(0o600)


async def _wait_for_pod_ssh(
    client: _PrimeIntellectClient,
    pod_id: str,
    *,
    timeout_s: int,
) -> _PodInfo:
    deadline = time.monotonic() + timeout_s
    last_status = "unknown"
    while time.monotonic() < deadline:
        pod = await asyncio.to_thread(client.get_pod, pod_id)
        status = str(pod.get("status") or "").upper()
        last_status = status or last_status
        if status in {"ERROR", "TERMINATED", "DELETING"}:
            raise RuntimeError(f"Prime Intellect sandbox {pod_id} entered status {status}")
        if status == "ACTIVE":
            connection = _parse_ssh_connection(pod)
            if connection is not None:
                return replace(connection, id=pod_id, status=status)
        await asyncio.sleep(5)
    raise TimeoutError(
        f"Prime Intellect sandbox {pod_id} did not become reachable within {timeout_s}s "
        f"(last status: {last_status})"
    )


async def _wait_for_ssh(connection: _PodInfo, private_key: Path, *, timeout_s: int) -> None:
    from ..ssh_exec import ssh_args

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        process = await asyncio.create_subprocess_exec(
            *ssh_args(
                connection.ssh_host,
                private_key,
                "true",
                user=connection.ssh_user,
                port=connection.ssh_port,
            ),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() == 0:
            return
        await asyncio.sleep(5)
    raise TimeoutError(
        f"Prime Intellect sandbox {connection.ssh_host}:{connection.ssh_port} "
        f"did not accept SSH within {timeout_s}s"
    )


def _parse_ssh_connection(pod: dict[str, object]) -> _PodInfo | None:
    raw_connection = pod.get("sshConnection")
    candidate = _first_string(raw_connection)
    if candidate is None:
        ip = _first_string(pod.get("ip"))
        if ip is None:
            return None
        return _PodInfo(id="", status="", ssh_user="root", ssh_host=ip, ssh_port=22)
    return _parse_ssh_invocation(candidate)


def _first_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _parse_ssh_invocation(connection: str) -> _PodInfo | None:
    text = connection.strip()
    match = _SSH_INVOCATION.match(text)
    tokens = (match.group("args") if match else text).split()
    user = "root"
    host: str | None = None
    port = 22
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-p" and index + 1 < len(tokens):
            try:
                port = int(tokens[index + 1])
            except ValueError:
                pass
            index += 2
            continue
        if token.startswith("-p") and len(token) > 2:
            try:
                port = int(token[2:])
            except ValueError:
                pass
            index += 1
            continue
        if token in {"-i", "-o", "-l", "-c", "-F", "-J"} and index + 1 < len(tokens):
            if token == "-l":
                user = tokens[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if "@" in token:
            user, host = token.split("@", 1)
        elif host is None:
            host = token
        index += 1
    if host is None:
        return None
    return _PodInfo(id="", status="", ssh_user=user, ssh_host=host, ssh_port=port)


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_IMAGE",
    "DEFAULT_WORKSPACE",
    "PrimeIntellectProvider",
    "PrimeIntellectSandbox",
    "_PrimeIntellectClient",
    "_PrimeIntellectConfig",
    "_Pod",
    "_PodInfo",
    "_SSHKey",
    "_parse_ssh_invocation",
]
