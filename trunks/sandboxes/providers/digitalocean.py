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
from dataclasses import dataclass
from pathlib import Path

from ..provider import (
    GPU,
    Isolation,
    NetworkMode,
    SandboxProviderInfo,
    SandboxRequest,
    Spec,
)
from ..ssh_exec import SshExecSandbox, ssh_args as _generic_ssh_args, _remote_script


DEFAULT_API_URL = "https://api.digitalocean.com"
DEFAULT_REGION = "nyc3"
DEFAULT_SIZE = "s-1vcpu-1gb"
DEFAULT_IMAGE = "ubuntu-24-04-x64"
DEFAULT_WORKSPACE = "/root/trunks"

_RUN_NAME = re.compile(r"[^a-z0-9-]+")
@dataclass(frozen=True)
class _DropletCatalogEntry:
    slug: str
    cpu: int
    memory_gib: int
    disk_gib: int
    gpu: GPU | None = None

    @property
    def spec(self) -> Spec:
        return Spec(cpu=self.cpu, memory_gib=self.memory_gib, disk_gib=self.disk_gib, gpu=self.gpu)


_DROPLET_CATALOG: tuple[_DropletCatalogEntry, ...] = (
    _DropletCatalogEntry("s-1vcpu-1gb", 1, 1, 25),
    _DropletCatalogEntry("s-1vcpu-2gb", 1, 2, 50),
    _DropletCatalogEntry("s-2vcpu-2gb", 2, 2, 60),
    _DropletCatalogEntry("s-2vcpu-4gb", 2, 4, 80),
    _DropletCatalogEntry("s-4vcpu-8gb", 4, 8, 160),
    _DropletCatalogEntry("s-8vcpu-16gb", 8, 16, 320),
    _DropletCatalogEntry("c-2", 2, 4, 25),
    _DropletCatalogEntry("c-4", 4, 8, 50),
    _DropletCatalogEntry("c-8", 8, 16, 100),
    _DropletCatalogEntry("c-16", 16, 32, 200),
    _DropletCatalogEntry("c-32", 32, 64, 400),
    _DropletCatalogEntry("c-48", 48, 96, 600),
    _DropletCatalogEntry("g-2vcpu-8gb", 2, 8, 25),
    _DropletCatalogEntry("g-4vcpu-16gb", 4, 16, 50),
    _DropletCatalogEntry("g-8vcpu-32gb", 8, 32, 100),
    _DropletCatalogEntry("g-16vcpu-64gb", 16, 64, 200),
    _DropletCatalogEntry("m-2vcpu-16gb", 2, 16, 50),
    _DropletCatalogEntry("m-4vcpu-32gb", 4, 32, 100),
    _DropletCatalogEntry("m-8vcpu-64gb", 8, 64, 200),
    _DropletCatalogEntry("m-16vcpu-128gb", 16, 128, 400),
    _DropletCatalogEntry("m-24vcpu-192gb", 24, 192, 600),
    _DropletCatalogEntry("m-32vcpu-256gb", 32, 256, 800),
    _DropletCatalogEntry("gpu-l40sx1-48gb", 8, 64, 500, gpu=GPU(kind="L40S_48GB", count=1)),
    _DropletCatalogEntry("gpu-l40sx8-384gb", 64, 768, 4000, gpu=GPU(kind="L40S_48GB", count=8)),
    _DropletCatalogEntry("gpu-h100x1-80gb", 20, 240, 720, gpu=GPU(kind="H100_80GB", count=1)),
    _DropletCatalogEntry("gpu-h100x8-640gb", 160, 1920, 5760, gpu=GPU(kind="H100_80GB", count=8)),
    _DropletCatalogEntry("gpu-mi300x1-192gb", 20, 240, 720, gpu=GPU(kind="MI300X_192GB", count=1)),
    _DropletCatalogEntry("gpu-mi300x8-1536gb", 160, 1536, 5760, gpu=GPU(kind="MI300X_192GB", count=8)),
)


@dataclass(frozen=True)
class _DigitalOceanConfig:
    token: str
    api_url: str
    region: str
    image: str
    workspace: str
    boot_timeout_s: int
    ssh_timeout_s: int
    allowed_specs: frozenset[str]


@dataclass(frozen=True)
class _SSHKey:
    id: int
    name: str


@dataclass(frozen=True)
class _Droplet:
    id: int
    name: str


class DigitalOceanProvider:
    def __init__(self, config: _DigitalOceanConfig, client: "_DigitalOceanClient | None" = None) -> None:
        self.config = config
        self.client = client or _DigitalOceanClient(config.token, config.api_url)
        catalog = _filter_catalog(_DROPLET_CATALOG, config.allowed_specs)
        if not catalog:
            raise LookupError("DIGITALOCEAN_ALLOWED_SPECS filtered out every available droplet size")
        self.catalog = catalog
        max_cpu = max(entry.cpu for entry in catalog)
        max_memory = max(entry.memory_gib for entry in catalog)
        max_disk = max(entry.disk_gib for entry in catalog)
        gpu_kinds = frozenset(entry.gpu.kind for entry in catalog if entry.gpu is not None)
        capabilities = {"linux", "remote", "vm"}
        if gpu_kinds:
            capabilities.add("gpu")
        self.info = SandboxProviderInfo(
            id="digitalocean",
            capabilities=frozenset(capabilities),
            isolation=frozenset({"vm"}),
            architectures=frozenset({"x86_64"}),
            network_modes=frozenset({"default"}),
            gpu_kinds=gpu_kinds,
            max_cpu=max_cpu,
            max_memory_gib=max_memory,
            max_disk_gib=max_disk,
            max_timeout_s=24 * 60 * 60,
            specs=tuple(entry.spec for entry in catalog),
            regions=frozenset({config.region}),
            secrets_passthrough=True,
            closure_fetch=True,
            artifact_upload=True,
            live_logs=True,
        )

    @classmethod
    async def discover(cls, config: dict[str, object]) -> "DigitalOceanProvider":
        if shutil.which("ssh") is None:
            raise LookupError("ssh CLI not found")
        if shutil.which("ssh-keygen") is None:
            raise LookupError("ssh-keygen CLI not found")
        parsed = _config(config)
        if parsed.token == "":
            raise LookupError("TRUNKS_DIGITALOCEAN_API_TOKEN or DIGITALOCEAN_API_TOKEN is not configured")
        return cls(parsed)

    async def supports(
        self,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: Isolation | None = None,
        network: NetworkMode = "default",
    ) -> bool:
        if not self.info.supports_static(
            capabilities=capabilities,
            spec=spec,
            region=region,
            isolation=isolation,
            network=network,
        ):
            return False
        return _select_catalog_entry(self.catalog, spec) is not None

    async def doctor(self) -> dict[str, object]:
        return {
            "api_url": self.config.api_url,
            "region": self.config.region,
            "image": self.config.image,
            "workspace": self.config.workspace,
            "ssh": "available",
            "catalog_source": "bundled",
            "catalog_size": len(self.catalog),
        }

    async def create(self, request: SandboxRequest) -> "DigitalOceanSandbox":
        entry = _select_catalog_entry(self.catalog, request.spec)
        if entry is None:
            raise RuntimeError(
                f"no DigitalOcean droplet size satisfies spec {request.spec.key}"
            )
        name = _resource_name(request.run)
        key_dir = Path(tempfile.mkdtemp(prefix="trunks-do-key-"))
        private_key = key_dir / "id_ed25519"
        public_key = key_dir / "id_ed25519.pub"
        ssh_key: _SSHKey | None = None
        droplet: _Droplet | None = None
        try:
            await _generate_keypair(private_key)
            ssh_key = await asyncio.to_thread(self.client.create_ssh_key, name, public_key.read_text(encoding="utf-8"))
            droplet = await asyncio.to_thread(
                self.client.create_droplet,
                name,
                region=self.config.region,
                size=entry.slug,
                image=self.config.image,
                ssh_key_id=ssh_key.id,
            )
            ip = await _wait_for_droplet_ip(self.client, droplet.id, timeout_s=self.config.boot_timeout_s)
            await _wait_for_ssh(ip, private_key, timeout_s=self.config.ssh_timeout_s)
            return DigitalOceanSandbox(
                request,
                client=self.client,
                droplet=droplet,
                ssh_key=ssh_key,
                private_key=private_key,
                key_dir=key_dir,
                ip=ip,
                workspace=self.config.workspace,
            )
        except Exception:
            if droplet is not None:
                await asyncio.to_thread(self.client.delete_droplet, droplet.id)
            if ssh_key is not None:
                await asyncio.to_thread(self.client.delete_ssh_key, ssh_key.id)
            shutil.rmtree(key_dir, ignore_errors=True)
            raise

    async def list_orphans(self, *, prefix: str = "trunks-") -> list[dict[str, object]]:
        droplets, keys = await asyncio.gather(
            asyncio.to_thread(self.client.list_droplets),
            asyncio.to_thread(self.client.list_ssh_keys),
        )
        orphans: list[dict[str, object]] = []
        for droplet in droplets:
            name = str(droplet.get("name") or "")
            if name.startswith(prefix):
                orphans.append(
                    {
                        "type": "droplet",
                        "id": droplet.get("id"),
                        "name": name,
                        "status": droplet.get("status"),
                        "region": _path(droplet, "region.slug"),
                    }
                )
        for key in keys:
            name = str(key.get("name") or "")
            if name.startswith(prefix):
                orphans.append(
                    {
                        "type": "ssh_key",
                        "id": key.get("id"),
                        "name": name,
                    }
                )
        return orphans

    async def cleanup_orphans(self, *, prefix: str = "trunks-", dry_run: bool = True) -> dict[str, object]:
        orphans = await self.list_orphans(prefix=prefix)
        deleted: list[dict[str, object]] = []
        if not dry_run:
            for item in orphans:
                resource_type = item.get("type")
                resource_id = item.get("id")
                if not isinstance(resource_id, int):
                    continue
                if resource_type == "droplet":
                    await asyncio.to_thread(self.client.delete_droplet, resource_id)
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


class DigitalOceanSandbox(SshExecSandbox):
    def __init__(
        self,
        request: SandboxRequest,
        *,
        client: "_DigitalOceanClient",
        droplet: _Droplet,
        ssh_key: _SSHKey,
        private_key: Path,
        key_dir: Path,
        ip: str,
        workspace: str,
    ) -> None:
        super().__init__(
            request,
            ip=ip,
            private_key_path=private_key,
            workspace=workspace,
            ssh_user="root",
            host_label="DigitalOcean",
        )
        self.client = client
        self.droplet = droplet
        self.ssh_key = ssh_key
        self.private_key = private_key
        self.key_dir = key_dir

    async def destroy(self) -> None:
        await self.cancel()
        await asyncio.gather(
            asyncio.to_thread(self.client.delete_droplet, self.droplet.id),
            asyncio.to_thread(self.client.delete_ssh_key, self.ssh_key.id),
        )
        shutil.rmtree(self.key_dir, ignore_errors=True)


class _DigitalOceanClient:
    def __init__(self, token: str, api_url: str = DEFAULT_API_URL) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def create_ssh_key(self, name: str, public_key: str) -> _SSHKey:
        response = self._request("POST", "/v2/account/keys", body={"name": name, "public_key": public_key})
        key = response.get("ssh_key")
        if not isinstance(key, dict) or not isinstance(key.get("id"), int):
            raise RuntimeError("DigitalOcean create SSH key response did not include ssh_key.id")
        return _SSHKey(id=int(key["id"]), name=str(key.get("name") or name))

    def delete_ssh_key(self, key_id: int) -> None:
        self._request("DELETE", f"/v2/account/keys/{key_id}", allow_404=True)

    def list_ssh_keys(self) -> list[dict[str, object]]:
        return self._paged("/v2/account/keys", "ssh_keys")

    def create_droplet(
        self,
        name: str,
        *,
        region: str,
        size: str,
        image: str,
        ssh_key_id: int,
    ) -> _Droplet:
        response = self._request(
            "POST",
            "/v2/droplets",
            body={
                "name": name,
                "region": region,
                "size": size,
                "image": image,
                "ssh_keys": [ssh_key_id],
                "backups": False,
                "ipv6": False,
                "monitoring": False,
                "tags": ["trunks"],
            },
        )
        droplet = response.get("droplet")
        if not isinstance(droplet, dict) or not isinstance(droplet.get("id"), int):
            raise RuntimeError("DigitalOcean create droplet response did not include droplet.id")
        return _Droplet(id=int(droplet["id"]), name=str(droplet.get("name") or name))

    def get_droplet(self, droplet_id: int) -> dict[str, object]:
        response = self._request("GET", f"/v2/droplets/{droplet_id}")
        droplet = response.get("droplet")
        if not isinstance(droplet, dict):
            raise RuntimeError(f"DigitalOcean droplet {droplet_id} response did not include droplet")
        return droplet

    def delete_droplet(self, droplet_id: int) -> None:
        self._request("DELETE", f"/v2/droplets/{droplet_id}", allow_404=True)

    def list_droplets(self, tag: str | None = None) -> list[dict[str, object]]:
        query = {"tag_name": tag} if tag else None
        return self._paged("/v2/droplets", "droplets", query=query)

    def list_regions(self) -> list[dict[str, object]]:
        return self._paged("/v2/regions", "regions")

    def list_sizes(self) -> list[dict[str, object]]:
        return self._paged("/v2/sizes", "sizes")

    def _paged(self, path: str, key: str, query: dict[str, str] | None = None) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        page = 1
        while True:
            params = dict(query or {})
            params.update({"page": str(page), "per_page": "200"})
            response = self._request("GET", path, query=params)
            raw_items = response.get(key)
            if not isinstance(raw_items, list):
                return items
            items.extend(item for item in raw_items if isinstance(item, dict))
            links = response.get("links")
            pages = links.get("pages") if isinstance(links, dict) else None
            if not isinstance(pages, dict) or not pages.get("next"):
                return items
            page += 1

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
            "Authorization": f"Bearer {self.token}",
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
            raise RuntimeError(f"DigitalOcean API {method} {path} failed: {exc.code} {details}") from exc
        if not raw:
            return {}
        parsed = json.loads(raw.decode())
        return parsed if isinstance(parsed, dict) else {"data": parsed}


def _config(config: dict[str, object]) -> _DigitalOceanConfig:
    token = (
        _config_str(config, "api_key")
        or os.environ.get("TRUNKS_DIGITALOCEAN_API_TOKEN")
        or os.environ.get("DIGITALOCEAN_API_TOKEN")
        or ""
    )
    size = _config_str(config, "size") or os.environ.get("DIGITALOCEAN_SIZE")
    allowed_raw = (
        _config_str(config, "allowed_specs")
        or os.environ.get("DIGITALOCEAN_ALLOWED_SPECS")
        or ""
    )
    allowed_specs: frozenset[str]
    if size:
        allowed_specs = frozenset({size})
    elif allowed_raw:
        allowed_specs = frozenset(item.strip() for item in allowed_raw.split(",") if item.strip())
    else:
        allowed_specs = frozenset()
    return _DigitalOceanConfig(
        token=token,
        api_url=_config_str(config, "api_url") or os.environ.get("DIGITALOCEAN_API_URL") or DEFAULT_API_URL,
        region=_config_str(config, "region") or os.environ.get("DIGITALOCEAN_REGION") or DEFAULT_REGION,
        image=_config_str(config, "image") or os.environ.get("DIGITALOCEAN_IMAGE") or DEFAULT_IMAGE,
        workspace=_config_str(config, "workspace") or os.environ.get("DIGITALOCEAN_WORKSPACE") or DEFAULT_WORKSPACE,
        boot_timeout_s=int(config.get("boot_timeout_s") or os.environ.get("DIGITALOCEAN_BOOT_TIMEOUT_S") or 300),
        ssh_timeout_s=int(config.get("ssh_timeout_s") or os.environ.get("DIGITALOCEAN_SSH_TIMEOUT_S") or 300),
        allowed_specs=allowed_specs,
    )


def _config_str(config: dict[str, object], key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def _filter_catalog(
    catalog: tuple[_DropletCatalogEntry, ...],
    allowed: frozenset[str],
) -> tuple[_DropletCatalogEntry, ...]:
    if not allowed:
        return catalog
    return tuple(
        entry
        for entry in catalog
        if entry.slug in allowed
        or entry.spec.key in allowed
        or (entry.gpu is not None and entry.gpu.kind in allowed)
    )


def _select_catalog_entry(
    catalog: tuple[_DropletCatalogEntry, ...],
    spec: Spec,
) -> _DropletCatalogEntry | None:
    candidates = [
        entry
        for entry in catalog
        if entry.cpu >= spec.cpu
        and entry.memory_gib >= spec.memory_gib
        and entry.disk_gib >= spec.disk_gib
        and _gpu_satisfies(entry.gpu, spec.gpu)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda entry: (entry.cpu, entry.memory_gib, entry.disk_gib))


def _gpu_satisfies(offered: GPU | None, requested: GPU | None) -> bool:
    if requested is None:
        return offered is None
    if offered is None:
        return False
    return offered.kind == requested.kind and offered.count >= requested.count


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


async def _wait_for_droplet_ip(client: _DigitalOceanClient, droplet_id: int, *, timeout_s: int) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        droplet = await asyncio.to_thread(client.get_droplet, droplet_id)
        if droplet.get("status") == "active":
            ip = _public_ipv4(droplet)
            if ip:
                return ip
        await asyncio.sleep(3)
    raise TimeoutError(f"DigitalOcean droplet {droplet_id} did not become reachable within {timeout_s}s")


async def _wait_for_ssh(ip: str, private_key: Path, *, timeout_s: int) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        process = await asyncio.create_subprocess_exec(
            *_ssh_args(ip, private_key, "true"),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() == 0:
            return
        await asyncio.sleep(3)
    raise TimeoutError(f"DigitalOcean droplet {ip} did not accept SSH within {timeout_s}s")


def _ssh_args(ip: str, private_key: Path, remote_command: str) -> list[str]:
    return _generic_ssh_args(ip, private_key, remote_command, user="root")


def _public_ipv4(droplet: dict[str, object]) -> str | None:
    networks = droplet.get("networks")
    v4 = networks.get("v4") if isinstance(networks, dict) else None
    if not isinstance(v4, list):
        return None
    for item in v4:
        if isinstance(item, dict) and item.get("type") == "public" and isinstance(item.get("ip_address"), str):
            return str(item["ip_address"])
    return None


def _resource_name(run: str) -> str:
    raw = _RUN_NAME.sub("-", run.lower()).strip("-") or uuid.uuid4().hex[:12]
    return f"trunks-{raw[:26]}-{uuid.uuid4().hex[:8]}"


def _path(data: object, path: str) -> object:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
