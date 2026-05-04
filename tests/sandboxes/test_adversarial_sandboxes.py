from __future__ import annotations

import asyncio
import tempfile
import time
import sys
import unittest
from uuid import uuid4
from pathlib import Path

from trunks.sandboxes import ExecResult, LogChunk, SandboxFile, SandboxRequest, Spec
from trunks.sandboxes.providers.docker import DockerSandbox
from trunks.sandboxes.providers.local import LocalSandbox


class DockerSandboxAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_command_does_not_put_secret_values_in_argv(self) -> None:
        request = SandboxRequest(
            run="run",
            commit="worktree",
            spec=Spec(cpu=1, memory_gib=1, disk_gib=1, arch="arm64"),
            region="local",
            isolation="container",
            network="default",
            timeout_s=30,
        )
        sandbox = DockerSandbox(request, image="ubuntu:24.04")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                token_value = f"token-{uuid4().hex}"
                command, env = sandbox._docker_command(["/bin/sh", "-lc", "printf \"$RUN_TOKEN\""], {"RUN_TOKEN": token_value}, tmp)
                self.assertNotIn(token_value, "\0".join(command))
                self.assertEqual(env["RUN_TOKEN"], token_value)
                self.assertIn("--env", command)
                self.assertIn("RUN_TOKEN", command)
        finally:
            await sandbox.destroy()


class LocalSandboxAdversarialTests(unittest.TestCase):
    def test_local_cancel_escalates_when_process_ignores_sigterm(self) -> None:
        asyncio.run(self._run_cancel_escalation_case())

    async def _run_cancel_escalation_case(self) -> None:
        request = SandboxRequest(
            run="run",
            commit="worktree",
            spec=Spec(cpu=1, memory_gib=1, disk_gib=1, arch="arm64"),
            region="local",
            isolation="process",
            network="default",
            timeout_s=30,
        )
        sandbox = LocalSandbox(request)
        with tempfile.TemporaryDirectory() as tmp:
            child = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('started', flush=True); time.sleep(10)"
            events = sandbox.run([sys.executable, "-c", child], {}, tmp, 30).__aiter__()
            first = await events.__anext__()
            self.assertIsInstance(first, LogChunk)
            started = time.monotonic()
            await sandbox.cancel()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
            result = await events.__anext__()
            self.assertIsInstance(result, ExecResult)
            await sandbox.destroy()


class DockerSandboxPathAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_upload_and_download_paths_cannot_escape_temp_root(self) -> None:
        request = SandboxRequest(
            run="run",
            commit="worktree",
            spec=Spec(cpu=1, memory_gib=1, disk_gib=1, arch="arm64"),
            region="local",
            isolation="container",
            network="default",
            timeout_s=30,
        )
        sandbox = DockerSandbox(request, image="ubuntu:24.04")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "payload.txt"
                source.write_text("payload", encoding="utf-8")
                with self.assertRaises(ValueError):
                    [item async for item in sandbox.upload((SandboxFile(str(source), "/../../escape.txt"),))]
                with self.assertRaises(ValueError):
                    [item async for item in sandbox.download((SandboxFile("/../../escape.txt", str(source)),))]
        finally:
            await sandbox.destroy()


if __name__ == "__main__":
    unittest.main()
