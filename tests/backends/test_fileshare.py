from __future__ import annotations

import tempfile
import unittest

from tests.contract.backend import assert_backend_contract
from trunks.backends.fileshare import FileShare


class FileShareBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await assert_backend_contract(self, FileShare(tmp))


if __name__ == "__main__":
    unittest.main()
