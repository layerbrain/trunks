from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.session import ChangedFile
from trunks.sessions.inprocess import InProcess


class InProcessSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_reports_files_under_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = InProcess(root)
            await session.start()
            await session.checkout([ChangedFile("README.md", b"old\n")])
            (root / "README.md").write_text("hi\n", encoding="utf-8")

            changed = {entry.path: entry.data for entry in [item async for item in session.save()]}

            self.assertEqual(changed, {"README.md": b"hi\n"})
            await session.destroy()

    async def test_exec_is_explicitly_not_supported_in_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = InProcess(tmp)
            await session.start()
            with self.assertRaises(NotImplementedError):
                await session.exec(["echo", "hi"], cwd=".", env={}, stdin=None)


if __name__ == "__main__":
    unittest.main()
