from __future__ import annotations

import os
import asyncio
import subprocess
import time
import unittest
from contextlib import contextmanager

from trunks.backends.postgres import Postgres
from trunks.credentials import PostgresCredentials
from tests.contract.backend import assert_backend_contract
from tests.contract.workflow import assert_workflow_contract


@contextmanager
def postgres_server():
    if subprocess.run(["docker", "--version"], capture_output=True).returncode != 0:
        raise unittest.SkipTest("docker is not available")
    name = f"trunks-postgres-{os.getpid()}-{int(time.time())}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=trunks",
            "-e",
            "POSTGRES_USER=trunks",
            "-e",
            "POSTGRES_DB=trunks",
            "-p",
            "0:5432",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        raise unittest.SkipTest(f"could not start Postgres: {run.stderr.strip()}")
    try:
        port = ""
        for _ in range(80):
            inspect = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True, text=True)
            if inspect.returncode == 0 and inspect.stdout.strip():
                port = inspect.stdout.strip().rsplit(":", 1)[-1]
                break
            time.sleep(0.2)
        if not port:
            raise RuntimeError("Postgres did not expose a port")
        yield f"postgres://trunks:trunks@127.0.0.1:{port}/trunks"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


class PostgresBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if os.environ.get("TRUNKS_DOCKER_TESTS") != "1":
            raise unittest.SkipTest("set TRUNKS_DOCKER_TESTS=1 to run Docker backend tests")
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_postgres_backend_contract(self) -> None:
        with postgres_server() as dsn:
            backend = Postgres(PostgresCredentials(dsn), trunk="repo")
            for _ in range(120):
                try:
                    await backend.capabilities()
                    break
                except Exception:
                    time.sleep(0.25)
            await assert_backend_contract(self, backend)
            await backend.__aexit__(None, None, None)

    async def test_workflow_contract_against_postgres(self) -> None:
        with postgres_server() as dsn:
            backend = Postgres(PostgresCredentials(dsn), trunk="workflow")
            for _ in range(120):
                try:
                    await backend.capabilities()
                    break
                except Exception:
                    time.sleep(0.25)
            try:
                await assert_workflow_contract(self, backend)
            finally:
                await backend.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
