from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.session import ChangedFile
from trunks.sessions.shell import Shell


class ShellSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkout_exec_and_save_are_filesystem_scoped_to_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = Shell(root)
            await session.start()
            await session.checkout([ChangedFile("existing.txt", b"old\n"), ChangedFile("delete.txt", b"gone\n")])

            result = await session.exec(
                [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "Path('existing.txt').write_text('new\\n'); "
                        "Path('out.txt').write_text('session\\n'); "
                        "Path('delete.txt').unlink()"
                    ),
                ],
                cwd=".",
                env={},
                stdin=None,
            )

            self.assertEqual(result.code, 0)
            self.assertEqual((root / "out.txt").read_text(encoding="utf-8"), "session\n")
            changed = {entry.path: entry.data for entry in [item async for item in session.save()]}
            self.assertEqual(changed["out.txt"], b"session\n")
            self.assertEqual(changed["existing.txt"], b"new\n")
            self.assertIsNone(changed["delete.txt"])
            await session.destroy()


if __name__ == "__main__":
    unittest.main()
