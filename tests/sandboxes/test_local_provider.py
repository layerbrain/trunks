from __future__ import annotations

import asyncio
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch
from trunks.sandboxes import ExecResult, LogChunk, SandboxFile, SandboxProviderInfo, SandboxRequest, Spec
from trunks.sandboxes.contract import run_provider_contract
from trunks.sandboxes.providers.docker import DockerProvider
from trunks.sandboxes.providers.local import LocalProvider
from trunks.sandboxes.registry import ProviderRegistry


def _docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class SandboxSyncApiTests(unittest.TestCase):
    def test_registry_and_contract_run_sync_outside_event_loop(self) -> None:
        registry = ProviderRegistry.discover()
        self.assertIsInstance(registry, ProviderRegistry)
        provider = registry.get("local")
        result = run_provider_contract(provider)
        self.assertFalse(hasattr(result, "__await__"))
        assert isinstance(result, dict)
        self.assertTrue(result["passed"])

    def test_provider_scaffold_generates_protocol_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = asyncio.run(dispatch(["sandboxes", "providers", "scaffold", "acme-fast", "-o", tmp, "--json"]))
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            path = Path(payload["path"])
            self.assertTrue(path.exists())
            self.assertIn("class AcmeFastProvider", path.read_text(encoding="utf-8"))


class LocalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_discovers_available_first_party_providers(self) -> None:
        registry = await ProviderRegistry.discover()
        providers = registry.list()
        provider_ids = [provider.id for provider in providers]
        self.assertIn("local", provider_ids)
        local = next(provider for provider in providers if provider.id == "local")
        self.assertEqual(local.network_modes, frozenset({"default"}))
        if _docker_available():
            self.assertIn("docker", provider_ids)

    async def test_local_provider_streams_logs_with_monotonic_sequence(self) -> None:
        provider = await LocalProvider.discover({})
        request = SandboxRequest(
            run="run",
            commit="worktree",
            spec=provider.info.specs[0],
            region="local",
            isolation="process",
            network="default",
            timeout_s=5,
        )
        sandbox = await provider.create(request)
        with tempfile.TemporaryDirectory() as tmp:
            events = [
                event
                async for event in sandbox.run(
                    ["/bin/sh", "-lc", "printf 'one\\n'; printf 'two\\n' >&2"],
                    {},
                    tmp,
                    5,
                )
            ]
        logs = [event for event in events if isinstance(event, LogChunk)]
        result = next(event for event in events if isinstance(event, ExecResult))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual([log.seq for log in logs], sorted(log.seq for log in logs))
        self.assertEqual({log.stream for log in logs}, {"stdout", "stderr"})

    async def test_local_provider_streams_logs_before_process_exits(self) -> None:
        provider = await LocalProvider.discover({})
        request = SandboxRequest(
            run="run",
            commit="worktree",
            spec=provider.info.specs[0],
            region="local",
            isolation="process",
            network="default",
            timeout_s=5,
        )
        sandbox = await provider.create(request)
        with tempfile.TemporaryDirectory() as tmp:
            started = asyncio.get_running_loop().time()
            events = sandbox.run(["/bin/sh", "-lc", "echo start; sleep 1; echo end"], {}, tmp, 5)
            first = await anext(events)
            self.assertIsInstance(first, LogChunk)
            self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
            remaining = [event async for event in events]
        self.assertTrue(any(isinstance(event, ExecResult) for event in remaining))
        await sandbox.destroy()

    async def test_local_timeout_kills_shell_children(self) -> None:
        provider = await LocalProvider.discover({})
        request = SandboxRequest(
            run="run",
            commit="worktree",
            spec=provider.info.specs[0],
            region="local",
            isolation="process",
            network="default",
            timeout_s=1,
        )
        sandbox = await provider.create(request)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                events = [
                    event
                    async for event in sandbox.run(
                        ["/bin/sh", "-lc", "sleep 2; printf orphan > orphan.txt"],
                        {},
                        tmp,
                        1,
                    )
                ]
                result = next(event for event in events if isinstance(event, ExecResult))
                self.assertTrue(result.timed_out)
                await asyncio.sleep(1.5)
                self.assertFalse((root / "orphan.txt").exists())
        finally:
            await sandbox.destroy()

    async def test_local_upload_download_streams_transfer_progress(self) -> None:
        provider = await LocalProvider.discover({})
        sandbox = await provider.create(
            SandboxRequest(
                run="run",
                commit="worktree",
                spec=provider.info.specs[0],
                region="local",
                isolation="process",
                network="default",
                timeout_s=5,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            target = root / "nested" / "target.txt"
            source.write_text("payload", encoding="utf-8")
            progress = [item async for item in sandbox.upload((SandboxFile(str(source), str(target)),))]
            self.assertEqual(target.read_text(encoding="utf-8"), "payload")
            self.assertEqual(progress[0].bytes_total, len("payload"))
            artifact = root / "artifact.txt"
            download = [item async for item in sandbox.download((SandboxFile(str(target), str(artifact)),))]
            self.assertEqual(artifact.read_text(encoding="utf-8"), "payload")
            self.assertEqual(download[0].bytes_done, len("payload"))

    async def test_local_provider_passes_contract(self) -> None:
        provider = await LocalProvider.discover({})
        result = await run_provider_contract(provider)
        self.assertTrue(result["passed"])
        self.assertEqual(result["_schema_version"], "trunks.sandboxes.contract.v1")

    async def test_specs_and_regions_can_filter_by_provider(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = await dispatch(["sandboxes", "specs", "--provider", "local", "--json"])
        self.assertEqual(code, 0)
        specs = json.loads(out.getvalue())
        self.assertEqual(specs["data"][0]["provider"], "local")
        spec = specs["data"][0]["specs"][0]
        self.assertEqual(spec["cpu"], 1)
        self.assertEqual(spec["memory_gib"], 1)
        self.assertEqual(spec["disk_gib"], 1)
        self.assertEqual(spec["key"], LocalProvider.info.specs[0].key)

        out = io.StringIO()
        with redirect_stdout(out):
            code = await dispatch(["sandboxes", "regions", "--provider", "local", "--json"])
        self.assertEqual(code, 0)
        regions = json.loads(out.getvalue())
        self.assertEqual(regions["data"][0]["regions"], ["local"])

    async def test_provider_doctor_and_benchmark(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = await dispatch(["sandboxes", "providers", "doctor", "local", "--json"])
        self.assertEqual(code, 0)
        doctor = json.loads(out.getvalue())
        self.assertTrue(doctor["ok"])

        out = io.StringIO()
        with redirect_stdout(out):
            code = await dispatch(["sandboxes", "providers", "benchmark", "local", "--json"])
        self.assertEqual(code, 0)
        benchmark = json.loads(out.getvalue())
        self.assertTrue(benchmark["contract"]["passed"])

    async def test_soft_provider_failure_falls_back_to_next_provider(self) -> None:
        class BrokenProvider:
            info = SandboxProviderInfo(
                id="broken",
                capabilities=frozenset({"macos", "linux", "windows"}),
                isolation=frozenset({"process"}),
                architectures=frozenset({"x86_64", "arm64"}),
                network_modes=frozenset({"default"}),
                gpu_kinds=frozenset(),
                max_cpu=99,
                max_memory_gib=99,
                max_disk_gib=99,
                max_timeout_s=99,
                specs=(Spec(cpu=1, memory_gib=1, disk_gib=1),),
                regions=frozenset({"local"}),
                secrets_passthrough=True,
                closure_fetch=True,
                artifact_upload=True,
                live_logs=True,
            )

            async def supports(self, *args: object) -> bool:
                raise RuntimeError("provider down")

        local = await LocalProvider.discover({})
        registry = ProviderRegistry({"broken": BrokenProvider(), "local": local})
        provider = await registry.resolve(
            capabilities=frozenset({next(iter(local.info.capabilities - {"local"}))}),
            spec=local.info.specs[0],
            region="local",
            isolation="process",
            network="default",
            provider="broken",
        )
        self.assertEqual(provider.info.id, "local")

    async def test_entrypoint_provider_discovery_skips_unavailable_provider(self) -> None:
        class UnavailableProvider:
            @classmethod
            async def discover(cls, config: dict[str, object]) -> object:
                raise LookupError("provider API is unavailable")

        with patch("trunks.sandboxes.registry._entry_point_providers", return_value={"unavailable": UnavailableProvider}):
            registry = await ProviderRegistry.discover()

        provider_ids = {provider.id for provider in registry.list()}
        self.assertIn("local", provider_ids)
        self.assertNotIn("unavailable", provider_ids)


@unittest.skipUnless(_docker_available(), "Docker daemon is not available")
class DockerProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_provider_passes_contract(self) -> None:
        provider = await DockerProvider.discover({})
        result = await run_provider_contract(provider)
        self.assertTrue(result["passed"])
        self.assertEqual(result["provider"], "docker")

    async def test_cli_can_run_command_in_docker_container(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = await dispatch(
                [
                    "actions",
                    "run",
                    "--provider",
                    "docker",
                    "--strict-provider",
                    "--isolation",
                    "container",
                    "--command",
                    "printf docker-container",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"]["provider"], "docker")
        self.assertEqual(payload["logs"][0]["text"], "docker-container")

if __name__ == "__main__":
    unittest.main()
