from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trunks.actions.executor import execute_once_async
from trunks.actions.health import provider_health_snapshot
from trunks.actions.run import enqueue_command
from trunks.repository import Repository
from trunks.sandboxes import (
    Arch,
    NetworkMode,
    Sandbox,
    SandboxProviderInfo,
    SandboxRequest,
    Spec,
)
from trunks.sandboxes.providers.local import LocalProvider
from trunks.sandboxes.registry import ProviderRegistry, RegisteredProvider


def _matching_info(provider_id: str, *, arch: Arch) -> SandboxProviderInfo:
    return SandboxProviderInfo(
        id=provider_id,
        capabilities=frozenset({"local", "linux", "macos"}),
        isolation=frozenset({"process"}),
        architectures=frozenset({arch}),
        network_modes=frozenset({"default"}),
        gpu_kinds=frozenset(),
        max_cpu=8,
        max_memory_gib=16,
        max_disk_gib=64,
        max_timeout_s=3600,
        specs=(Spec(cpu=1, memory_gib=1, disk_gib=1, arch=arch),),
        regions=frozenset({"local"}),
        secrets_passthrough=True,
        closure_fetch=True,
        artifact_upload=True,
        live_logs=True,
    )


class _CrashingProvider:
    def __init__(self, provider_id: str, *, arch: Arch, error: str) -> None:
        self.info = _matching_info(provider_id, arch=arch)
        self._error = error
        self.create_calls = 0

    async def supports(
        self,
        capabilities: frozenset[str],
        spec: Spec,
        region: str | None,
        isolation: object | None = None,
        network: NetworkMode = "default",
    ) -> bool:
        return self.info.supports_static(
            capabilities=capabilities,
            spec=spec,
            region=region,
            isolation=isolation,
            network=network,
        )

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.create_calls += 1
        raise RuntimeError(self._error)


class ProviderFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_walks_chain_when_first_provider_create_raises(self) -> None:
        local = await LocalProvider.discover({})
        crashing = _CrashingProvider(
            "crashing-primary",
            arch=next(iter(local.info.architectures)),
            error="primary boot failed",
        )
        registry = ProviderRegistry(
            [
                RegisteredProvider(name="crashing-primary", type="crashing", priority=10, provider=crashing),
                RegisteredProvider(name="local", type="local", priority=20, provider=local),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="failover")
            pending = enqueue_command(repo, "printf failover-success > result.txt")

            async def _stub_discover(cls, config=None, *, profiles=None):
                return registry

            with patch.object(ProviderRegistry, "discover_async", classmethod(_stub_discover)):
                result = await execute_once_async(repo, executor="failover-executor", cwd=str(root))

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["id"], pending.id)
            state = result["state"]
            assert isinstance(state, dict)
            self.assertEqual(state["phase"], "succeeded")
            self.assertEqual(state["provider"], "local")
            self.assertEqual(crashing.create_calls, 1)
            self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "failover-success")

            snapshot = provider_health_snapshot(repo)
            providers_with_failures = {
                entry["provider"] for entry in snapshot["data"] if entry.get("failures")
            }
            self.assertIn("crashing-primary", providers_with_failures)
            self.assertNotIn("local", providers_with_failures)

    async def test_executor_seals_run_failed_when_every_provider_create_raises(self) -> None:
        local = await LocalProvider.discover({})
        arch = next(iter(local.info.architectures))
        first = _CrashingProvider("crash-first", arch=arch, error="first boot failed")
        second = _CrashingProvider("crash-second", arch=arch, error="second boot failed")
        registry = ProviderRegistry(
            [
                RegisteredProvider(name="crash-first", type="crashing", priority=10, provider=first),
                RegisteredProvider(name="crash-second", type="crashing", priority=20, provider=second),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="failover-dead")
            pending = enqueue_command(repo, "printf never-runs > result.txt")

            async def _stub_discover(cls, config=None, *, profiles=None):
                return registry

            with patch.object(ProviderRegistry, "discover_async", classmethod(_stub_discover)):
                result = await execute_once_async(repo, executor="dead-executor", cwd=str(root))

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["id"], pending.id)
            state = result["state"]
            assert isinstance(state, dict)
            self.assertEqual(state["phase"], "failed")
            self.assertEqual(first.create_calls, 1)
            self.assertEqual(second.create_calls, 1)
            self.assertFalse((root / "result.txt").exists())

            snapshot = provider_health_snapshot(repo)
            providers_with_failures = {
                entry["provider"] for entry in snapshot["data"] if entry.get("failures")
            }
            self.assertIn("crash-first", providers_with_failures)
            self.assertIn("crash-second", providers_with_failures)


if __name__ == "__main__":
    unittest.main()
