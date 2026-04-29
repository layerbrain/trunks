from __future__ import annotations

import asyncio
import tempfile
import unittest

from tests.contract.workflow import assert_workflow_contract
from trunks.backends.fileshare import FileShare
from trunks.backends.local import Local
from trunks.backends.memory import Memory
from trunks.backends.sqlite import SQLite


class WorkflowContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_workflow_against_memory_backend(self) -> None:
        await assert_workflow_contract(self, Memory())

    async def test_workflow_against_local_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await assert_workflow_contract(self, Local(f"{tmp}/local"))

    async def test_workflow_against_fileshare_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await assert_workflow_contract(self, FileShare(f"{tmp}/fileshare"))

    async def test_workflow_against_sqlite_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await assert_workflow_contract(self, SQLite(f"{tmp}/backend.db", trunk="workflow"))


if __name__ == "__main__":
    unittest.main()
