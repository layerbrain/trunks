from __future__ import annotations

import unittest

from tests.contract.backend import assert_backend_contract
from trunks.backends.fileshare import FileShare
from trunks.backends.local import Local
from trunks.backends.memory import Memory
from trunks.backends.sqlite import SQLite


class FakeS3(Memory):
    pass


class FakeAzure(Memory):
    pass


class FakeGCS(Memory):
    pass


class FakePostgres(Memory):
    pass


class FakeSFTP(Memory):
    pass


class BackendContractMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import asyncio

        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_backend_contract_for_fast_backends(self) -> None:
        with self.subTest("memory"):
            await assert_backend_contract(self, Memory())
        with self.subTest("fake-s3"):
            await assert_backend_contract(self, FakeS3())
        with self.subTest("fake-azure"):
            await assert_backend_contract(self, FakeAzure())
        with self.subTest("fake-gcs"):
            await assert_backend_contract(self, FakeGCS())
        with self.subTest("fake-postgres"):
            await assert_backend_contract(self, FakePostgres())
        with self.subTest("fake-sftp"):
            await assert_backend_contract(self, FakeSFTP())

    async def test_backend_contract_for_fast_durable_backends(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.subTest("local"):
                await assert_backend_contract(self, Local(f"{tmp}/local"))
            with self.subTest("fileshare"):
                await assert_backend_contract(self, FileShare(f"{tmp}/fileshare"))
            with self.subTest("sqlite"):
                await assert_backend_contract(self, SQLite(f"{tmp}/backend.db", trunk="repo"))


if __name__ == "__main__":
    unittest.main()
