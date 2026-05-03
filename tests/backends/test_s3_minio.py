from __future__ import annotations

import os
import asyncio
import subprocess
import time
import unittest
from contextlib import contextmanager
from urllib.request import urlopen

from trunks.backends.s3 import S3
from trunks.credentials import S3Credentials
from trunks.chunked import CHUNK_THRESHOLD
from trunks.errors import BackendUnavailable
from trunks.objects import Blob
from tests.contract.backend import assert_backend_contract
from tests.contract.workflow import assert_workflow_contract


@contextmanager
def minio_server():
    docker = subprocess.run(["docker", "--version"], capture_output=True, text=True)
    if docker.returncode != 0:
        raise RuntimeError("docker is required to run MinIO backend tests")

    name = f"trunks-minio-{os.getpid()}-{int(time.time())}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "MINIO_ROOT_USER=minioadmin",
            "-e",
            "MINIO_ROOT_PASSWORD=minioadmin",
            "-p",
            "0:9000",
            "minio/minio:latest",
            "server",
            "/data",
            "--console-address",
            ":9001",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        raise RuntimeError(f"could not start MinIO: {run.stderr.strip()}")

    try:
        port = ""
        for _ in range(60):
            inspect = subprocess.run(
                ["docker", "port", name, "9000/tcp"],
                capture_output=True,
                text=True,
            )
            if inspect.returncode == 0 and inspect.stdout.strip():
                port = inspect.stdout.strip().rsplit(":", 1)[-1]
                break
            time.sleep(0.2)
        if not port:
            raise RuntimeError("MinIO did not expose a port")
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                with urlopen(f"{endpoint}/minio/health/ready", timeout=1):
                    break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("MinIO did not become ready")
        yield endpoint
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


class S3MinioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_s3_backend_contract_against_minio(self) -> None:
        with minio_server() as endpoint:
            backend = S3(
                bucket="trunks-test",
                prefix="repo.trunk",
                endpoint=endpoint,
                region="us-east-1",
                credentials=S3Credentials("minioadmin", "minioadmin"),
            )
            await backend.ensure_bucket()
            await assert_backend_contract(self, backend)

    async def test_workflow_contract_against_minio(self) -> None:
        with minio_server() as endpoint:
            backend = S3(
                bucket="trunks-workflow",
                prefix="workflow.trunk",
                endpoint=endpoint,
                region="us-east-1",
                credentials=S3Credentials("minioadmin", "minioadmin"),
            )
            await backend.ensure_bucket()
            await assert_workflow_contract(self, backend)

    async def test_s3_batch_write_uses_segment_layout(self) -> None:
        with minio_server() as endpoint:
            backend = S3(
                bucket="trunks-segment-test",
                prefix="repo.trunk",
                endpoint=endpoint,
                region="us-east-1",
                credentials=S3Credentials("minioadmin", "minioadmin"),
            )
            await backend.ensure_bucket()
            blobs = [Blob.from_data(f"payload-{index}".encode()) for index in range(8)]

            await backend.write_objects((blob.id, blob.canonical()) for blob in blobs)

            keys = await backend._list_keys("repo.trunk/")
            self.assertTrue(any(key.endswith(".tseg") for key in keys))
            self.assertTrue(any(key.endswith(".tidx") for key in keys))
            self.assertFalse(any("/objects/" in key for key in keys))
            for blob in blobs:
                self.assertEqual(await backend.read_object(blob.id), blob.canonical())

    async def test_s3_large_object_uses_chunk_layout(self) -> None:
        with minio_server() as endpoint:
            backend = S3(
                bucket="trunks-chunk-test",
                prefix="repo.trunk",
                endpoint=endpoint,
                region="us-east-1",
                credentials=S3Credentials("minioadmin", "minioadmin"),
            )
            await backend.ensure_bucket()
            blob = Blob.from_data(b"x" * (CHUNK_THRESHOLD + 17))

            await backend.write_object(blob.id, blob.canonical())

            keys = await backend._list_keys("repo.trunk/")
            self.assertTrue(any(key.endswith(".tmanifest") for key in keys))
            chunk_keys = [key for key in keys if "/chunks/" in key]
            self.assertGreater(len(chunk_keys), 1)
            self.assertEqual(await backend.read_object(blob.id), blob.canonical())

            await backend._request("PUT", chunk_keys[0], b"corrupt", {})
            with self.assertRaisesRegex(BackendUnavailable, "corrupt chunked object"):
                await backend.read_object(blob.id)
            await backend.write_object(blob.id, blob.canonical())
            self.assertEqual(await backend.read_object(blob.id), blob.canonical())


if __name__ == "__main__":
    unittest.main()
