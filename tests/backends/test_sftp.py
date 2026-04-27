from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.contract.backend import assert_backend_contract
from trunks.backends.sftp import SFTP
from trunks.credentials import SFTPCredentials


@contextmanager
def sftp_server():
    if subprocess.run(["docker", "--version"], capture_output=True).returncode != 0:
        raise unittest.SkipTest("docker is not available")
    with TemporaryDirectory() as tmp:
        key = Path(tmp) / "id_ed25519"
        keygen = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            capture_output=True,
            text=True,
        )
        if keygen.returncode != 0:
            raise unittest.SkipTest(f"could not generate SSH key: {keygen.stderr.strip()}")
        name = f"trunks-sftp-{os.getpid()}-{int(time.time())}"
        run = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-p",
                "127.0.0.1:0:22",
                "-v",
                f"{key}.pub:/home/foo/.ssh/keys/id_ed25519.pub:ro",
                "atmoz/sftp:latest",
                "foo::1001:1001:upload",
            ],
            capture_output=True,
            text=True,
        )
        if run.returncode != 0:
            raise unittest.SkipTest(f"could not start SFTP: {run.stderr.strip()}")
        try:
            port = ""
            for _ in range(80):
                inspect = subprocess.run(["docker", "port", name, "22/tcp"], capture_output=True, text=True)
                if inspect.returncode == 0 and inspect.stdout.strip():
                    port = inspect.stdout.strip().rsplit(":", 1)[-1]
                    break
                time.sleep(0.2)
            if not port:
                raise RuntimeError("SFTP did not expose a port")
            for _ in range(80):
                try:
                    with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
                        time.sleep(3)
                        break
                except OSError:
                    time.sleep(0.25)
            else:
                raise RuntimeError("SFTP did not become ready")
            yield int(port), key
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


class SFTPBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if os.environ.get("TRUNKS_DOCKER_TESTS") != "1":
            raise unittest.SkipTest("set TRUNKS_DOCKER_TESTS=1 to run Docker backend tests")
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_sftp_backend_contract(self) -> None:
        with sftp_server() as (port, key):
            backend = SFTP(
                host="127.0.0.1",
                port=port,
                username="foo",
                credentials=SFTPCredentials(ssh_key=key),
                root="/upload/repo.trunk",
            )
            try:
                await assert_backend_contract(self, backend)
            finally:
                await backend.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
