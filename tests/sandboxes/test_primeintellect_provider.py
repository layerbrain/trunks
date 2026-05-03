from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.sandboxes import GPU, SandboxRequest, Spec
from trunks.sandboxes.cli import dispatch
from trunks.sandboxes.providers.primeintellect import (
    DEFAULT_API_URL,
    DEFAULT_IMAGE,
    DEFAULT_WORKSPACE,
    PrimeIntellectProvider,
    PrimeIntellectSandbox,
    _Pod,
    _PodInfo,
    _PrimeIntellectConfig,
    _SSHKey,
    _parse_ssh_invocation,
)
from trunks.sandboxes.registry import ProviderRegistry


PRIME_KEY_ENV = "TRUNKS_PRIMEINTELLECT_API_KEY"


def _config(allowed: str = "") -> _PrimeIntellectConfig:
    return _PrimeIntellectConfig(
        api_key="test-key",
        api_url=DEFAULT_API_URL,
        region=None,
        data_center_id=None,
        country=None,
        image=DEFAULT_IMAGE,
        workspace=DEFAULT_WORKSPACE,
        allowed_specs=frozenset(item.strip() for item in allowed.split(",") if item.strip()),
        boot_timeout_s=1,
        ssh_timeout_s=1,
    )


class FakePodClient:
    def __init__(self, *, sshConnection: object = "ssh root@10.0.0.1 -p 12345") -> None:
        self.sshConnection = sshConnection
        self.deleted_pods: list[str] = []
        self.deleted_keys: list[str] = []
        self.create_pod_error: Exception | None = None
        self.created_pod_body: dict[str, object] | None = None
        self.find_availability_error: Exception | None = None
        self._pod_status_progression: list[str] = ["ACTIVE"]

    def upload_ssh_key(self, name: str, public_key: str) -> _SSHKey:
        self.uploaded_key_name = name
        self.uploaded_public_key = public_key
        return _SSHKey(id="key-1", name=name)

    def delete_ssh_key(self, key_id: str) -> None:
        self.deleted_keys.append(key_id)

    def list_ssh_keys(self) -> list[dict[str, object]]:
        return [
            {"id": "key-A", "name": "trunks-old"},
            {"id": "key-B", "name": "personal"},
        ]

    def list_pods(self) -> list[dict[str, object]]:
        return [
            {"id": "pod-A", "name": "trunks-old", "status": "ACTIVE"},
            {"id": "pod-B", "name": "research", "status": "ACTIVE"},
        ]

    def find_availability(
        self,
        *,
        gpu_type: str,
        gpu_count: int,
        region: str | None,
        data_center_id: str | None,
    ) -> dict[str, object]:
        if self.find_availability_error is not None:
            raise self.find_availability_error
        self.requested_availability = (gpu_type, gpu_count, region, data_center_id)
        return {
            "cloudId": "cloud-42",
            "gpuType": gpu_type,
            "socket": "PCIe",
            "provider": "primeintellect",
            "dataCenterId": "dc-1",
            "country": "US",
        }

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
        if self.create_pod_error is not None:
            raise self.create_pod_error
        self.created_pod_body = {
            "name": name,
            "availability": availability,
            "spec": spec,
            "run": run,
            "ssh_key_id": ssh_key_id,
            "image": image,
            "country": country,
        }
        return _Pod(id="pod-99", name=name)

    def get_pod(self, pod_id: str) -> dict[str, object]:
        status = self._pod_status_progression[0]
        if len(self._pod_status_progression) > 1:
            self._pod_status_progression.pop(0)
        return {
            "id": pod_id,
            "status": status,
            "sshConnection": self.sshConnection,
        }

    def delete_pod(self, pod_id: str) -> None:
        self.deleted_pods.append(pod_id)


async def _fake_keypair(path: Path) -> None:
    path.write_text("private", encoding="utf-8")
    path.with_suffix(path.suffix + ".pub").write_text("ssh-ed25519 public trunks\n", encoding="utf-8")


async def _no_wait(*args: object, **kwargs: object) -> None:
    return None


class PrimeIntellectProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_discovers_provider_only_when_key_is_configured(self) -> None:
        with patch.dict(os.environ, {PRIME_KEY_ENV: ""}, clear=False):
            registry = await ProviderRegistry.discover_async(
                {"providers": {"primeintellect": {"enabled": True}}}
            )
            self.assertNotIn("primeintellect", {info.id for info in registry.list()})

        with patch.dict(os.environ, {PRIME_KEY_ENV: "test-key"}, clear=False):
            registry = await ProviderRegistry.discover_async()
            provider = registry.get("primeintellect")
            self.assertEqual(provider.info.id, "primeintellect")
            self.assertEqual(provider.info.isolation, frozenset({"vm"}))
            self.assertIn("H100_80GB", provider.info.gpu_kinds)
            self.assertGreater(len(provider.info.specs), 5)

    async def test_supports_rejects_specs_without_a_gpu(self) -> None:
        provider = PrimeIntellectProvider(_config(), FakePodClient())
        gpu_spec = Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H100_80GB", count=1))
        self.assertTrue(
            await provider.supports(
                frozenset({"linux", "gpu"}),
                gpu_spec,
                None,
                isolation="vm",
            )
        )
        cpu_only = Spec(cpu=2, memory_gib=4, disk_gib=20)
        self.assertFalse(
            await provider.supports(
                frozenset({"linux"}),
                cpu_only,
                None,
                isolation="vm",
            )
        )

    async def test_allowed_specs_filter_restricts_catalog_to_listed_gpu_kinds(self) -> None:
        provider = PrimeIntellectProvider(_config(allowed="H100_80GB"), FakePodClient())
        kinds = {spec.gpu.kind for spec in provider.info.specs if spec.gpu is not None}
        self.assertEqual(kinds, {"H100_80GB"})
        a100x1 = Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="A100_80GB", count=1))
        self.assertFalse(
            await provider.supports(
                frozenset({"linux", "gpu"}),
                a100x1,
                None,
                isolation="vm",
            )
        )

    async def test_create_passes_spec_and_run_through_to_create_pod_body(self) -> None:
        client = FakePodClient()
        provider = PrimeIntellectProvider(_config(), client)
        with patch(
            "trunks.sandboxes.providers.primeintellect._generate_keypair",
            _fake_keypair,
        ), patch(
            "trunks.sandboxes.providers.primeintellect._wait_for_ssh",
            _no_wait,
        ):
            sandbox = await provider.create(
                SandboxRequest(
                    run="RUN_ABC",
                    commit="worktree",
                    spec=Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H100_80GB", count=1)),
                    region="us",
                    isolation="vm",
                    network="default",
                    timeout_s=600,
                )
            )
        self.assertIsInstance(sandbox, PrimeIntellectSandbox)
        self.assertEqual(client.requested_availability, ("H100_80GB", 1, "us", None))
        self.assertIsNotNone(client.created_pod_body)
        body = client.created_pod_body
        assert body is not None
        self.assertEqual(body["run"], "RUN_ABC")
        self.assertEqual(body["image"], DEFAULT_IMAGE)
        spec = body["spec"]
        assert isinstance(spec, Spec) and spec.gpu is not None
        self.assertEqual(spec.gpu.kind, "H100_80GB")
        self.assertEqual(sandbox.ssh_user, "root")
        self.assertEqual(sandbox.ip, "10.0.0.1")
        self.assertEqual(sandbox.ssh_port, 12345)

    async def test_create_cleans_up_when_pod_creation_fails(self) -> None:
        client = FakePodClient()
        client.create_pod_error = RuntimeError("quota exceeded")
        provider = PrimeIntellectProvider(_config(), client)
        with patch(
            "trunks.sandboxes.providers.primeintellect._generate_keypair",
            _fake_keypair,
        ):
            with self.assertRaises(RuntimeError):
                await provider.create(
                    SandboxRequest(
                        run="RUN_FAIL",
                        commit="worktree",
                        spec=Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H100_80GB", count=1)),
                        region=None,
                        isolation="vm",
                        network="default",
                        timeout_s=600,
                    )
                )
        self.assertEqual(client.deleted_keys, ["key-1"])
        self.assertEqual(client.deleted_pods, [])

    async def test_create_cleans_up_when_availability_lookup_fails(self) -> None:
        client = FakePodClient()
        client.find_availability_error = RuntimeError("no capacity")
        provider = PrimeIntellectProvider(_config(), client)
        with patch(
            "trunks.sandboxes.providers.primeintellect._generate_keypair",
            _fake_keypair,
        ):
            with self.assertRaises(RuntimeError):
                await provider.create(
                    SandboxRequest(
                        run="RUN_NA",
                        commit="worktree",
                        spec=Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H100_80GB", count=1)),
                        region=None,
                        isolation="vm",
                        network="default",
                        timeout_s=600,
                    )
                )
        self.assertEqual(client.deleted_keys, ["key-1"])
        self.assertEqual(client.deleted_pods, [])

    async def test_destroy_deletes_pod_and_key_and_removes_local_keydir(self) -> None:
        client = FakePodClient()
        provider = PrimeIntellectProvider(_config(), client)
        with patch(
            "trunks.sandboxes.providers.primeintellect._generate_keypair",
            _fake_keypair,
        ), patch(
            "trunks.sandboxes.providers.primeintellect._wait_for_ssh",
            _no_wait,
        ):
            sandbox = await provider.create(
                SandboxRequest(
                    run="RUN_OK",
                    commit="worktree",
                    spec=Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H100_80GB", count=1)),
                    region=None,
                    isolation="vm",
                    network="default",
                    timeout_s=600,
                )
            )
        key_dir = sandbox.key_dir
        self.assertTrue(key_dir.exists())
        await sandbox.destroy()
        self.assertEqual(client.deleted_pods, ["pod-99"])
        self.assertEqual(client.deleted_keys, ["key-1"])
        self.assertFalse(key_dir.exists())

    async def test_cleanup_orphans_only_acts_on_trunks_prefixed_resources(self) -> None:
        provider = PrimeIntellectProvider(_config(), FakePodClient())
        dry = await provider.cleanup_orphans(prefix="trunks-", dry_run=True)
        self.assertEqual({item["id"] for item in dry["orphans"]}, {"pod-A", "key-A"})
        real = await provider.cleanup_orphans(prefix="trunks-", dry_run=False)
        self.assertEqual(
            {(item["type"], item["id"]) for item in real["deleted"]},
            {("pod", "pod-A"), ("ssh_key", "key-A")},
        )

    async def test_doctor_reports_static_catalog_without_network_calls(self) -> None:
        provider = PrimeIntellectProvider(_config(), FakePodClient())
        checks = await provider.doctor()
        self.assertEqual(checks["api_url"], DEFAULT_API_URL)
        self.assertEqual(checks["workspace"], DEFAULT_WORKSPACE)
        self.assertEqual(checks["catalog_source"], "bundled")
        self.assertEqual(checks["catalog_size"], len(provider.info.specs))

    async def test_cli_show_uses_registered_provider_metadata(self) -> None:
        out = io.StringIO()
        with patch.dict(os.environ, {PRIME_KEY_ENV: "test-key"}, clear=False):
            with redirect_stdout(out):
                code = await dispatch(["providers", "show", "primeintellect", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["id"], "primeintellect")
        self.assertIn("H100_80GB", payload["gpu_kinds"])


class PrimeIntellectSshParserTests(unittest.TestCase):
    def test_parses_user_host_with_explicit_port_short_flag(self) -> None:
        info = _parse_ssh_invocation("ssh root@1.2.3.4 -p 22001")
        self.assertEqual(info, _PodInfo(id="", status="", ssh_user="root", ssh_host="1.2.3.4", ssh_port=22001))

    def test_parses_port_flag_in_either_order(self) -> None:
        info = _parse_ssh_invocation("ssh -p 50022 ubuntu@host.example")
        self.assertEqual(info, _PodInfo(id="", status="", ssh_user="ubuntu", ssh_host="host.example", ssh_port=50022))

    def test_skips_options_with_arguments_like_i_o_l(self) -> None:
        info = _parse_ssh_invocation(
            "ssh -i /tmp/key -o StrictHostKeyChecking=no -p 2222 root@example.com"
        )
        self.assertEqual(info.ssh_user, "root")
        self.assertEqual(info.ssh_host, "example.com")
        self.assertEqual(info.ssh_port, 2222)

    def test_l_flag_overrides_user(self) -> None:
        info = _parse_ssh_invocation("ssh -l ubuntu host.example")
        self.assertEqual(info.ssh_user, "ubuntu")
        self.assertEqual(info.ssh_host, "host.example")

    def test_returns_none_when_no_host_present(self) -> None:
        self.assertIsNone(_parse_ssh_invocation("ssh -p 22"))


class PrimeIntellectSandboxConstructorTests(unittest.TestCase):
    def test_remote_script_does_not_put_secret_values_in_ssh_arguments(self) -> None:
        from trunks.sandboxes.ssh_exec import _remote_script

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "id_ed25519"
            key.write_text("private", encoding="utf-8")
            sandbox = PrimeIntellectSandbox(
                SandboxRequest(
                    run="run",
                    commit="worktree",
                    spec=Spec(cpu=8, memory_gib=32, disk_gib=100, gpu=GPU(kind="H100_80GB", count=1)),
                    region=None,
                    isolation="vm",
                    network="default",
                    timeout_s=10,
                ),
                client=FakePodClient(),
                pod=_Pod(id="pod-1", name="trunks-run"),
                ssh_key=_SSHKey(id="key-1", name="trunks-run"),
                private_key=key,
                key_dir=Path(tmp),
                ssh_user="root",
                ssh_host="203.0.113.10",
                ssh_port=12345,
                workspace="/root/trunks",
            )
            argv = sandbox._ssh_args("sh -s -- /bin/sh -lc 'printf \"$RUN_TOKEN\"'")
            secret = "secret-token-xyz"
            script = _remote_script("/root/trunks", {"RUN_TOKEN": secret})
            self.assertNotIn(secret, "\0".join(argv))
            self.assertIn(secret, script)
            self.assertIn("12345", argv)


if __name__ == "__main__":
    unittest.main()
