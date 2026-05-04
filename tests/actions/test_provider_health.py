from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.actions.health import degraded_provider_ids, provider_health_snapshot, record_provider_failure, record_provider_success
from trunks.repository import Repository
from trunks.sandboxes import SandboxProviderInfo, Spec
from trunks.sandboxes.registry import ProviderRegistry


class DummyProvider:
    def __init__(self, provider_id: str) -> None:
        self.info = SandboxProviderInfo(
            id=provider_id,
            capabilities=frozenset({"linux"}),
            isolation=frozenset({"container"}),
            architectures=frozenset({"x86_64"}),
            network_modes=frozenset({"default"}),
            gpu_kinds=frozenset(),
            max_cpu=8,
            max_memory_gib=16,
            max_disk_gib=100,
            max_timeout_s=3600,
            specs=(Spec(cpu=1, memory_gib=1, disk_gib=1),),
            regions=frozenset({"us"}),
            secrets_passthrough=True,
            closure_fetch=True,
            artifact_upload=True,
            live_logs=False,
        )

    async def supports(self, capabilities, spec, region, isolation=None, network="default"):
        return True


class ProviderHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_degraded_provider_is_routed_after_healthy_candidates(self) -> None:
        spec = Spec(cpu=1, memory_gib=1, disk_gib=1)
        registry = ProviderRegistry({"bad": DummyProvider("bad"), "good": DummyProvider("good")})  # type: ignore[arg-type]

        provider = await registry.resolve(
            capabilities=frozenset({"linux"}),
            spec=spec,
            region="us",
            isolation="container",
            network="default",
            avoid=frozenset({"bad"}),
        )

        self.assertEqual(provider.info.id, "good")

    async def test_health_refs_degrade_then_recover_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(cwd=Path(tmp), name="demo")
            spec_key = Spec(cpu=1, memory_gib=1, disk_gib=1).key

            record_provider_failure(
                repo,
                provider="daytona",
                region="us",
                spec_key=spec_key,
                run="run-1",
                reason="timeout",
                now_s=100,
            )
            self.assertEqual(degraded_provider_ids(repo, region="us", spec_key=spec_key, now_s=120), frozenset({"daytona"}))
            self.assertEqual(degraded_provider_ids(repo, region="us", spec_key=spec_key, now_s=200), frozenset())

            record_provider_success(repo, provider="daytona", region="us", spec_key=spec_key, run="run-2", now_s=130)
            self.assertEqual(degraded_provider_ids(repo, region="us", spec_key=spec_key, now_s=140), frozenset())
            snapshot = provider_health_snapshot(repo)
            self.assertEqual(snapshot["data"][0]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
