from __future__ import annotations

import asyncio
import os
import subprocess
import time
import unittest
from contextlib import contextmanager
from urllib.request import urlopen

from trunks.backends.azure import Azure
from trunks.credentials import AzureCredentials
from tests.contract.backend import assert_backend_contract

AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


@contextmanager
def azurite_server():
    if subprocess.run(["docker", "--version"], capture_output=True).returncode != 0:
        raise unittest.SkipTest("docker is not available")
    name = f"trunks-azurite-{os.getpid()}-{int(time.time())}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "0:10000",
            "mcr.microsoft.com/azure-storage/azurite:latest",
            "azurite-blob",
            "--blobHost",
            "0.0.0.0",
            "--skipApiVersionCheck",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        raise unittest.SkipTest(f"could not start Azurite: {run.stderr.strip()}")
    try:
        port = ""
        for _ in range(80):
            inspect = subprocess.run(["docker", "port", name, "10000/tcp"], capture_output=True, text=True)
            if inspect.returncode == 0 and inspect.stdout.strip():
                port = inspect.stdout.strip().rsplit(":", 1)[-1]
                break
            time.sleep(0.2)
        if not port:
            raise RuntimeError("Azurite did not expose a port")
        endpoint = f"http://127.0.0.1:{port}/{AZURITE_ACCOUNT}"
        for _ in range(120):
            try:
                urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        yield endpoint, AZURITE_KEY
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


class AzureAzuriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if os.environ.get("TRUNKS_DOCKER_TESTS") != "1":
            raise unittest.SkipTest("set TRUNKS_DOCKER_TESTS=1 to run Docker backend tests")
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_azure_backend_contract_against_azurite(self) -> None:
        with azurite_server() as (endpoint, key):
            backend = Azure(
                account=AZURITE_ACCOUNT,
                container="repo",
                prefix="repo.trunk",
                endpoint=endpoint,
                credentials=AzureCredentials(account_name=AZURITE_ACCOUNT, account_key=key),
            )
            try:
                await backend.ensure_container()
                await assert_backend_contract(self, backend)
            finally:
                await backend.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
