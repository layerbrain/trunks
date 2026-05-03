from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from trunks.sandboxes import GPU, SandboxRequest, Spec
from trunks.sandboxes.cli import dispatch
from trunks.sandboxes.providers.digitalocean import (
    DEFAULT_API_URL,
    DEFAULT_IMAGE,
    DEFAULT_REGION,
    DEFAULT_WORKSPACE,
    DigitalOceanProvider,
    DigitalOceanSandbox,
    _DigitalOceanConfig,
    _DROPLET_CATALOG,
    _Droplet,
    _SSHKey,
    _remote_script,
)
from trunks.sandboxes.registry import ProviderRegistry


def _config(allowed_specs: frozenset[str] = frozenset()) -> _DigitalOceanConfig:
    return _DigitalOceanConfig(
        token="test-token",
        api_url=DEFAULT_API_URL,
        region=DEFAULT_REGION,
        image=DEFAULT_IMAGE,
        workspace=DEFAULT_WORKSPACE,
        boot_timeout_s=1,
        ssh_timeout_s=1,
        allowed_specs=allowed_specs,
    )


class FakeDigitalOceanClient:
    def __init__(self) -> None:
        self.deleted_droplets: list[int] = []
        self.deleted_keys: list[int] = []
        self.create_droplet_error: Exception | None = None

    def create_ssh_key(self, name: str, public_key: str) -> _SSHKey:
        self.key_name = name
        self.public_key = public_key
        return _SSHKey(id=10, name=name)

    def create_droplet(self, name: str, *, region: str, size: str, image: str, ssh_key_id: int) -> _Droplet:
        if self.create_droplet_error is not None:
            raise self.create_droplet_error
        self.droplet_name = name
        self.region = region
        self.size = size
        self.image = image
        self.ssh_key_id = ssh_key_id
        return _Droplet(id=20, name=name)

    def delete_droplet(self, droplet_id: int) -> None:
        self.deleted_droplets.append(droplet_id)

    def delete_ssh_key(self, key_id: int) -> None:
        self.deleted_keys.append(key_id)

    def list_droplets(self, tag: str | None = None) -> list[dict[str, object]]:
        return [
            {"id": 1, "name": "trunks-run-one", "status": "active", "region": {"slug": "nyc3"}},
            {"id": 2, "name": "prod-db", "status": "active", "region": {"slug": "nyc3"}},
        ]

    def list_ssh_keys(self) -> list[dict[str, object]]:
        return [
            {"id": 3, "name": "trunks-run-one"},
            {"id": 4, "name": "laptop"},
        ]


async def _fake_keypair(path: Path) -> None:
    path.write_text("private", encoding="utf-8")
    path.with_suffix(path.suffix + ".pub").write_text("ssh-ed25519 public trunks\n", encoding="utf-8")


def _async_return(value: object):
    async def _fn(*args: object, **kwargs: object) -> object:
        return value

    return _fn


async def _async_noop(*args: object, **kwargs: object) -> None:
    return None


class DigitalOceanProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_discovers_digitalocean_only_when_token_is_configured(self) -> None:
        with patch.dict(os.environ, {"TRUNKS_DIGITALOCEAN_API_TOKEN": "", "DIGITALOCEAN_API_TOKEN": ""}, clear=False):
            registry = await ProviderRegistry.discover_async({"providers": {"digitalocean": {"enabled": True}}})
            self.assertNotIn("digitalocean", {provider.id for provider in registry.list()})

        with patch.dict(os.environ, {"TRUNKS_DIGITALOCEAN_API_TOKEN": "test-token"}, clear=False):
            registry = await ProviderRegistry.discover_async()
            provider = registry.get("digitalocean")
            self.assertEqual(provider.info.id, "digitalocean")
            self.assertEqual(provider.info.isolation, frozenset({"vm"}))

    async def test_provider_supports_vm_specs_and_rejects_impossible_specs(self) -> None:
        provider = DigitalOceanProvider(_config(), FakeDigitalOceanClient())
        self.assertTrue(
            await provider.supports(
                frozenset({"linux"}),
                Spec(cpu=1, memory_gib=1, disk_gib=1),
                "nyc3",
                isolation="vm",
            )
        )
        self.assertTrue(
            await provider.supports(
                frozenset({"linux"}),
                Spec(cpu=2, memory_gib=2, disk_gib=50),
                "nyc3",
                isolation="vm",
            )
        )
        self.assertFalse(
            await provider.supports(
                frozenset({"linux"}),
                Spec(cpu=999, memory_gib=1, disk_gib=1),
                "nyc3",
                isolation="vm",
            )
        )
        self.assertFalse(
            await provider.supports(
                frozenset({"linux"}),
                Spec(cpu=1, memory_gib=1, disk_gib=1, network="none"),
                "nyc3",
                isolation="vm",
                network="none",
            )
        )
        self.assertFalse(
            await provider.supports(
                frozenset({"linux"}),
                Spec(cpu=1, memory_gib=1, disk_gib=1),
                "sfo3",
                isolation="vm",
            )
        )

    async def test_info_exposes_catalog_specs_and_gpu_kinds(self) -> None:
        provider = DigitalOceanProvider(_config(), FakeDigitalOceanClient())
        self.assertIn("gpu", provider.info.capabilities)
        self.assertIn("H100_80GB", provider.info.gpu_kinds)
        self.assertIn("MI300X_192GB", provider.info.gpu_kinds)
        self.assertIn("L40S_48GB", provider.info.gpu_kinds)
        self.assertGreaterEqual(provider.info.max_cpu, 160)
        self.assertGreaterEqual(provider.info.max_memory_gib, 1536)

    async def test_provider_supports_gpu_specs_when_catalog_has_gpu(self) -> None:
        provider = DigitalOceanProvider(_config(), FakeDigitalOceanClient())
        self.assertTrue(
            await provider.supports(
                frozenset({"linux", "gpu"}),
                Spec(cpu=20, memory_gib=240, disk_gib=720, gpu=GPU(kind="H100_80GB", count=1)),
                "nyc3",
                isolation="vm",
            )
        )
        self.assertFalse(
            await provider.supports(
                frozenset({"linux", "gpu"}),
                Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="A100_80GB", count=1)),
                "nyc3",
                isolation="vm",
            )
        )

    async def test_allowed_specs_filter_restricts_catalog_to_listed_entries(self) -> None:
        provider = DigitalOceanProvider(_config(allowed_specs=frozenset({"s-1vcpu-1gb"})), FakeDigitalOceanClient())
        self.assertEqual(len(provider.catalog), 1)
        self.assertEqual(provider.catalog[0].slug, "s-1vcpu-1gb")
        self.assertEqual(provider.info.gpu_kinds, frozenset())
        self.assertNotIn("gpu", provider.info.capabilities)

    async def test_allowed_specs_filter_accepts_gpu_kind(self) -> None:
        provider = DigitalOceanProvider(_config(allowed_specs=frozenset({"H100_80GB"})), FakeDigitalOceanClient())
        slugs = {entry.slug for entry in provider.catalog}
        self.assertEqual(slugs, {"gpu-h100x1-80gb", "gpu-h100x8-640gb"})

    async def test_allowed_specs_empty_after_filter_raises_lookup_error(self) -> None:
        with self.assertRaises(LookupError):
            DigitalOceanProvider(_config(allowed_specs=frozenset({"nonexistent-slug"})), FakeDigitalOceanClient())

    async def test_create_picks_smallest_matching_droplet_slug(self) -> None:
        client = FakeDigitalOceanClient()
        provider = DigitalOceanProvider(_config(), client)
        with patch("trunks.sandboxes.providers.digitalocean._generate_keypair", _fake_keypair), patch(
            "trunks.sandboxes.providers.digitalocean._wait_for_droplet_ip",
            new=_async_return("203.0.113.10"),
        ), patch(
            "trunks.sandboxes.providers.digitalocean._wait_for_ssh",
            new=_async_noop,
        ):
            sandbox = await provider.create(
                SandboxRequest(
                    run="run-pick-slug",
                    commit="worktree",
                    spec=Spec(cpu=2, memory_gib=2, disk_gib=50),
                    region="nyc3",
                    isolation="vm",
                    network="default",
                    timeout_s=10,
                )
            )
        self.assertEqual(client.size, "s-2vcpu-2gb")
        self.assertEqual(sandbox.droplet.id, 20)

    async def test_create_picks_gpu_droplet_slug_for_h100_spec(self) -> None:
        client = FakeDigitalOceanClient()
        provider = DigitalOceanProvider(_config(), client)
        with patch("trunks.sandboxes.providers.digitalocean._generate_keypair", _fake_keypair), patch(
            "trunks.sandboxes.providers.digitalocean._wait_for_droplet_ip",
            new=_async_return("203.0.113.11"),
        ), patch(
            "trunks.sandboxes.providers.digitalocean._wait_for_ssh",
            new=_async_noop,
        ):
            sandbox = await provider.create(
                SandboxRequest(
                    run="run-h100",
                    commit="worktree",
                    spec=Spec(cpu=20, memory_gib=240, disk_gib=720, gpu=GPU(kind="H100_80GB", count=1)),
                    region="nyc3",
                    isolation="vm",
                    network="default",
                    timeout_s=10,
                )
            )
        self.assertEqual(client.size, "gpu-h100x1-80gb")
        self.assertEqual(sandbox.droplet.id, 20)

    async def test_create_raises_when_no_catalog_entry_satisfies_spec(self) -> None:
        client = FakeDigitalOceanClient()
        provider = DigitalOceanProvider(_config(), client)
        with patch("trunks.sandboxes.providers.digitalocean._generate_keypair", _fake_keypair):
            with self.assertRaises(RuntimeError) as caught:
                await provider.create(
                    SandboxRequest(
                        run="run-impossible",
                        commit="worktree",
                        spec=Spec(cpu=4096, memory_gib=4096, disk_gib=4096),
                        region="nyc3",
                        isolation="vm",
                        network="default",
                        timeout_s=10,
                    )
                )
        self.assertIn("no DigitalOcean droplet size satisfies spec", str(caught.exception))

    async def test_create_cleans_up_ssh_key_when_droplet_creation_fails(self) -> None:
        client = FakeDigitalOceanClient()
        client.create_droplet_error = RuntimeError("quota exceeded")
        provider = DigitalOceanProvider(_config(), client)
        with patch("trunks.sandboxes.providers.digitalocean._generate_keypair", _fake_keypair):
            with self.assertRaises(RuntimeError):
                await provider.create(
                    SandboxRequest(
                        run="RUN_123",
                        commit="worktree",
                        spec=Spec(cpu=1, memory_gib=1, disk_gib=1),
                        region="nyc3",
                        isolation="vm",
                        network="default",
                        timeout_s=10,
                    )
                )
        self.assertEqual(client.deleted_keys, [10])
        self.assertEqual(client.deleted_droplets, [])

    async def test_cleanup_orphans_deletes_only_trunks_prefixed_droplets_and_keys(self) -> None:
        client = FakeDigitalOceanClient()
        provider = DigitalOceanProvider(_config(), client)
        dry = await provider.cleanup_orphans(prefix="trunks-", dry_run=True)
        self.assertEqual([item["id"] for item in dry["orphans"]], [1, 3])
        self.assertEqual(client.deleted_droplets, [])
        self.assertEqual(client.deleted_keys, [])

        real = await provider.cleanup_orphans(prefix="trunks-", dry_run=False)
        self.assertEqual(real["deleted"], [{"type": "droplet", "id": 1}, {"type": "ssh_key", "id": 3}])
        self.assertEqual(client.deleted_droplets, [1])
        self.assertEqual(client.deleted_keys, [3])

    async def test_doctor_reports_static_catalog_without_network_calls(self) -> None:
        provider = DigitalOceanProvider(_config(), FakeDigitalOceanClient())
        checks = await provider.doctor()
        self.assertEqual(checks["region"], "nyc3")
        self.assertEqual(checks["workspace"], "/root/trunks")
        self.assertEqual(checks["catalog_source"], "bundled")
        self.assertEqual(checks["catalog_size"], len(provider.catalog))

    async def test_cli_show_uses_registered_provider_metadata(self) -> None:
        out = io.StringIO()
        with patch.dict(os.environ, {"TRUNKS_DIGITALOCEAN_API_TOKEN": "test-token"}, clear=False):
            with redirect_stdout(out):
                code = await dispatch(["providers", "show", "digitalocean", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["id"], "digitalocean")
        self.assertEqual(payload["isolation"], ["vm"])
        self.assertEqual(len(payload["specs"]), len(_DROPLET_CATALOG))
        self.assertIn("key", payload["specs"][0])

    async def test_cli_remote_provider_contract_requires_live_flag(self) -> None:
        err = io.StringIO()
        with patch.dict(os.environ, {"TRUNKS_DIGITALOCEAN_API_TOKEN": "test-token"}, clear=False):
            with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
                await dispatch(["providers", "test", "digitalocean", "--json"])
        self.assertNotEqual(caught.exception.code, 0)


class DigitalOceanSandboxTests(unittest.TestCase):
    def test_remote_script_does_not_put_secret_values_in_ssh_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "id_ed25519"
            key.write_text("private", encoding="utf-8")
            sandbox = DigitalOceanSandbox(
                SandboxRequest(
                    run="run",
                    commit="worktree",
                    spec=Spec(cpu=1, memory_gib=1, disk_gib=1),
                    region="nyc3",
                    isolation="vm",
                    network="default",
                    timeout_s=10,
                ),
                client=FakeDigitalOceanClient(),
                droplet=_Droplet(id=20, name="trunks-run"),
                ssh_key=_SSHKey(id=10, name="trunks-run"),
                private_key=key,
                key_dir=Path(tmp),
                ip="203.0.113.10",
                workspace="/root/trunks",
            )
            token_value = f"token-{uuid4().hex}"
            argv = sandbox._ssh_args("sh -s -- /bin/sh -lc 'printf \"$RUN_TOKEN\"'")
            script = _remote_script("/root/trunks", {"RUN_TOKEN": token_value})
            self.assertNotIn(token_value, "\0".join(argv))
            self.assertIn(token_value, script)

    def test_remote_script_rejects_invalid_environment_names(self) -> None:
        with self.assertRaises(ValueError):
            _remote_script("/root/trunks", {"BAD-NAME": "value"})


if __name__ == "__main__":
    unittest.main()
