from __future__ import annotations

import unittest

from trunks.backends.memory import Memory
from tests.contract.backend import assert_backend_contract


class MemoryBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_contract(self) -> None:
        backend = Memory()
        await assert_backend_contract(self, backend)


if __name__ == "__main__":
    unittest.main()
