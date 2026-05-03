from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from trunks.sandboxes import SandboxRequest, Spec
from trunks.sandboxes.contract import run_provider_contract
from trunks.sandboxes.providers.daytona import (
    DEFAULT_API_URL,
    DEFAULT_TOOLBOX_URL,
    DaytonaProvider,
    _DaytonaClient,
    _DaytonaConfig,
)
from trunks.sandboxes.registry import ProviderRegistry


DAYTONA_KEY_ENV = "TRUNKS_DAYTONA_API_KEY"


def _config() -> _DaytonaConfig:
    return _DaytonaConfig(
        api_key="test-key",
        api_url="https://api.test",
        toolbox_url="https://toolbox.test",
        organization_id=None,
        region=None,
        snapshot=None,
    )


class DaytonaProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_discovers_daytona_only_when_key_is_configured(self) -> None:
        with patch.dict(os.environ, {DAYTONA_KEY_ENV: "", "DAYTONA_API_KEY": ""}, clear=False):
            registry = await ProviderRegistry.discover_async({"providers": {"daytona": {"enabled": True}}})
            self.assertNotIn("daytona", {info.id for info in registry.list()})

        with patch.dict(os.environ, {DAYTONA_KEY_ENV: "test-key"}, clear=False):
            registry = await ProviderRegistry.discover_async()
            provider = registry.get("daytona")
            self.assertEqual(provider.info.id, "daytona")
            self.assertEqual(provider.info.isolation, frozenset({"container"}))

    async def test_daytona_contract_uses_python_provider_without_sdk_or_json_definition(self) -> None:
        requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        uploads: dict[str, bytes] = {}
        destroyed: list[str] = []

        def request(client, method, url, *, body, content_type, headers):
            requests.append((method, url, body, headers))
            self.assertEqual(headers["Authorization"], "Bearer test-key")
            self.assertNotIn("X-Daytona-Organization-ID", headers)
            if method == "POST" and url == "https://api.test/sandbox":
                payload = json.loads((body or b"{}").decode())
                self.assertNotIn("cpu", payload)
                self.assertNotIn("memory", payload)
                self.assertNotIn("disk", payload)
                self.assertNotIn("image", payload)
                self.assertNotIn("target", payload)
                self.assertEqual(payload["labels"]["trunks_provider"], "daytona")
                return b'{"id":"sandbox-123"}'
            if method == "GET" and url == "https://toolbox.test/sandbox-123/work-dir":
                return b'{"dir":"/work"}'
            if method == "POST" and url == "https://toolbox.test/sandbox-123/files/folder?path=%2Ftmp&mode=0755":
                return b"{}"
            if method == "POST" and url == "https://toolbox.test/sandbox-123/files/upload?path=%2Ftmp%2Ftrunks-contract-input.txt":
                uploads[url] = body or b""
                self.assertTrue(content_type and content_type.startswith("multipart/form-data; boundary="))
                return b"{}"
            if method == "POST" and url == "https://toolbox.test/sandbox-123/process/execute":
                payload = json.loads((body or b"{}").decode())
                self.assertEqual(payload["cwd"], "/tmp")
                self.assertIn("cat /tmp/trunks-contract-input.txt", payload["command"])
                self.assertIn("envs", payload)
                return b'{"result":{"stdout":"contract-input\\n","stderr":"","exitCode":0}}'
            if method == "GET" and url == "https://toolbox.test/sandbox-123/files/download?path=%2Ftmp%2Ftrunks-contract-output.txt":
                return b"contract-output\n"
            if method == "DELETE" and url == "https://api.test/sandbox/sandbox-123":
                destroyed.append("sandbox-123")
                return b""
            raise AssertionError(f"unexpected request: {method} {url}")

        provider = DaytonaProvider(_config())
        with patch.object(_DaytonaClient, "request", request):
            result = await run_provider_contract(provider)

        self.assertTrue(result["passed"])
        self.assertTrue(uploads)
        self.assertEqual(destroyed, ["sandbox-123"])
        self.assertEqual(requests[0][0], "POST")

    async def test_daytona_configured_region_is_supported(self) -> None:
        provider = DaytonaProvider(
            _DaytonaConfig(
                api_key="test-key",
                api_url=DEFAULT_API_URL,
                toolbox_url=DEFAULT_TOOLBOX_URL,
                organization_id=None,
                region="us",
                snapshot=None,
            )
        )
        self.assertTrue(
            await provider.supports(
                frozenset({"linux", "container", "remote"}),
                provider.info.specs[0],
                "us",
                isolation="container",
            )
        )
        self.assertFalse(
            await provider.supports(
                frozenset({"linux", "container", "remote"}),
                provider.info.specs[0],
                "eu",
                isolation="container",
            )
        )

    async def test_create_sends_organization_header_when_configured(self) -> None:
        seen: list[dict[str, str]] = []

        def request(client, method, url, *, body, content_type, headers):
            seen.append(dict(headers))
            if method == "POST" and url == "https://api.test/sandbox":
                return b'{"id":"sandbox-123"}'
            raise AssertionError(f"unexpected request: {method} {url}")

        provider = DaytonaProvider(
            _DaytonaConfig(
                api_key="test-key",
                api_url="https://api.test",
                toolbox_url="https://toolbox.test",
                organization_id="org-1",
                region=None,
                snapshot="snapshot-1",
            )
        )
        with patch.object(_DaytonaClient, "request", request):
            sandbox = await provider.create(
                SandboxRequest(
                    run="RUN-XYZ",
                    commit="worktree",
                    spec=Spec(cpu=2, memory_gib=4, disk_gib=8),
                    region="global",
                    isolation="container",
                    network="default",
                    timeout_s=10,
                )
            )
        self.assertEqual(sandbox.sandbox_id, "sandbox-123")
        self.assertEqual(seen[0]["X-Daytona-Organization-ID"], "org-1")

    async def test_daytona_orphan_cleanup_lists_and_deletes_only_trunks_named_sandboxes(self) -> None:
        deleted: list[str] = []

        def request(client, method, url, *, body, content_type, headers):
            if method == "GET" and url == "https://api.test/sandbox":
                self.assertIsNone(body)
                self.assertIsNone(content_type)
                return json.dumps(
                    {
                        "sandboxes": [
                            {"id": "one", "name": "trunks-run-one", "status": "stopped", "labels": {"trunks_run_id": "run-one"}},
                            {"id": "two", "name": "not-ours", "status": "started"},
                        ]
                    }
                ).encode()
            if method == "DELETE" and url == "https://api.test/sandbox/one":
                deleted.append("one")
                return b""
            raise AssertionError(f"unexpected request: {method} {url}")

        provider = DaytonaProvider(_config())
        with patch.object(_DaytonaClient, "request", request):
            dry = await provider.cleanup_orphans(prefix="trunks-", dry_run=True)
            real = await provider.cleanup_orphans(prefix="trunks-", dry_run=False)

        self.assertEqual([item["id"] for item in dry["orphans"]], ["one"])
        self.assertEqual(deleted, ["one"])
        self.assertEqual(real["deleted"], ["one"])

    async def test_doctor_reports_static_catalog_without_network_calls(self) -> None:
        provider = DaytonaProvider(_config())
        checks = await provider.doctor()
        self.assertEqual(checks["api_url"], "https://api.test")
        self.assertEqual(checks["toolbox_url"], "https://toolbox.test")
        self.assertEqual(checks["catalog_source"], "bundled")
        self.assertEqual(checks["catalog_size"], 1)


if __name__ == "__main__":
    unittest.main()
