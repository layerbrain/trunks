from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.cli import dispatch
from trunks.objects import Blob
from trunks.repository import Repository


class CheckCleanTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_clean_removes_orphan_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "a.md").write_text("a\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)

                repo = Repository.find(root)
                # Inject an unreachable blob — same shape that a stray pre-shim
                # `git commit` could leave behind in `.git/objects/`.
                orphan = Blob.from_data(b"orphan payload that nothing references\n")
                repo.put_object(orphan.id, orphan.canonical())
                self.assertEqual(repo.object_data(orphan.id), orphan.canonical())

                removed = repo.clean_unreachable()
                self.assertGreaterEqual(removed, 1)

                from trunks.errors import ObjectNotFound
                with self.assertRaises(ObjectNotFound):
                    repo.object_data(orphan.id)
                # Reachable history still intact
                head = repo.ref("main")
                self.assertIsNotNone(head)
                assert head is not None
                repo.load_commit(head)
            finally:
                os.chdir(cwd)

    async def test_check_clean_idempotent_on_clean_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "a.md").write_text("a\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                repo = Repository.find(root)
                self.assertEqual(repo.clean_unreachable(), 0)
                self.assertEqual(repo.clean_unreachable(), 0)
            finally:
                os.chdir(cwd)

    async def test_cli_check_clean_reports_count_in_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "a.md").write_text("a\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)

                repo = Repository.find(root)
                orphan = Blob.from_data(b"sweep-me\n")
                repo.put_object(orphan.id, orphan.canonical())

                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["check", "--clean", "--json"]), 0)
                payload = json.loads(out.getvalue())
                self.assertTrue(payload["ok"])
                self.assertGreaterEqual(payload["removed_unreachable_objects"], 1)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
