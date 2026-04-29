from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from trunks.cli import dispatch


class DiffBeforeFirstCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_diff_before_first_commit_treats_main_as_empty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                (root / "a.md").write_text("a\n", encoding="utf-8")
                (root / "b.md").write_text("b\n", encoding="utf-8")
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["diff", "--json"]), 0)
                self.assertEqual(
                    json.loads(out.getvalue()),
                    [{"path": "a.md", "status": "A"}, {"path": "b.md", "status": "A"}],
                )
            finally:
                os.chdir(cwd)

    async def test_diff_with_explicit_unknown_ref_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                (root / "a.md").write_text("a\n", encoding="utf-8")
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    rc = await dispatch(["diff", "--vs", "does-not-exist"])
                self.assertEqual(rc, 1)
                self.assertIn("unknown ref or commit", err.getvalue())
                self.assertIn("does-not-exist", err.getvalue())
            finally:
                os.chdir(cwd)

    async def test_diff_text_output_before_first_commit_lists_adds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                (root / "task.md").write_text("hi\n", encoding="utf-8")
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["diff"]), 0)
                self.assertEqual(out.getvalue().strip(), "A task.md")
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
