from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.engine import Engine
from trunks.repository import Repository


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_commit_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            engine = Engine(repo)
            await engine.write("src/app.py", b"print('hi')\n")
            commit = await engine.commit(message="add app")
            self.assertEqual(repo.ref("main"), commit.id)
            self.assertEqual(await engine.read("src/app.py"), b"print('hi')\n")


if __name__ == "__main__":
    unittest.main()

