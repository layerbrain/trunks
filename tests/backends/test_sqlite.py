from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.backends.sqlite import SQLite
from tests.contract.backend import assert_backend_contract


class SQLiteBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = SQLite(Path(tmp) / "backend.db", trunk="repo")
            await assert_backend_contract(self, backend)


if __name__ == "__main__":
    unittest.main()
