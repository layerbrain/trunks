from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import unittest
from contextlib import contextmanager

from tests.contract.backend import assert_backend_contract
from tests.contract.workflow import assert_workflow_contract
from trunks.backends.gcs import GCS


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextmanager
def fake_gcs_server():
    if subprocess.run(["docker", "--version"], capture_output=True).returncode != 0:
        raise unittest.SkipTest("docker is not available")
    name = f"trunks-gcs-{os.getpid()}-{int(time.time())}"
    port = free_port()
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:4443",
            "fsouza/fake-gcs-server:latest",
            "-backend",
            "memory",
            "-scheme",
            "http",
            "-port",
            "4443",
            "-public-host",
            f"127.0.0.1:{port}",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        raise unittest.SkipTest(f"could not start fake-gcs-server: {run.stderr.strip()}")
    try:
        time.sleep(1)
        yield f"http://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


class GCSFakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if os.environ.get("TRUNKS_DOCKER_TESTS") != "1":
            raise unittest.SkipTest("set TRUNKS_DOCKER_TESTS=1 to run Docker backend tests")
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_gcs_backend_contract_against_fake_gcs(self) -> None:
        with fake_gcs_server() as endpoint:
            backend = GCS(bucket="repo", prefix="repo.trunk", endpoint=endpoint)
            await backend.ensure_bucket()
            await assert_backend_contract(self, backend)

    async def test_workflow_contract_against_fake_gcs(self) -> None:
        with fake_gcs_server() as endpoint:
            backend = GCS(bucket="workflow", prefix="workflow.trunk", endpoint=endpoint)
            await backend.ensure_bucket()
            await assert_workflow_contract(self, backend)


if __name__ == "__main__":
    unittest.main()
