"""
Regression test for the silent-push bug.

Reproduction:
- A trunks repo has a remote configured (so trunks push isn't a noop early-exit)
- BUT the local trunks SQLite has zero refs (the gitshim never copied anything in,
  e.g. because the user ran git push outside the trunks-managed shell)
- `trunks push` previously printed nothing and exited 0 — looks identical to a
  successful 7-commit push to the user. Bytes silently land nowhere.

This test asserts:
1. Push.run() returns a PushResult with all-zero counts in that case.
2. The CLI `run_trunk("push")` prints a clear "did you commit through the gitshim?"
   message instead of staying silent.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.backends.local import Local
from trunks.cli import run_trunk
from trunks.engine import Engine
from trunks.push import Push, PushResult
from trunks.repository import Repository


class PushVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_run_returns_zero_counts_when_no_local_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp, name="empty-repo")
            backend = Local(Path(tmp) / "remote.trunk")
            async with backend:
                result = await Push(repo, backend).run()
            self.assertIsInstance(result, PushResult)
            self.assertEqual(result.refs_pushed, 0)
            self.assertEqual(result.refs_already_current, 0)
            self.assertEqual(result.objects_uploaded, 0)

    async def test_engine_push_returns_result_when_data_flows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp, name="real-repo")
            backend = Local(Path(tmp) / "remote.trunk")
            async with backend:
                engine = Engine(repo, backend)
                await engine.write("README.md", b"# hi\n")
                await engine.commit(message="init")
                result = await engine.push()
            self.assertIsNotNone(result)
            assert result is not None
            self.assertGreaterEqual(result.refs_pushed, 1)
            self.assertGreater(result.objects_uploaded, 0)

    async def test_cli_push_prints_warning_when_no_local_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                repo = Repository.init(tmp, name="silent-repo")
                repo.set_backend_url(str(Path(tmp) / "remote.trunk"))
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await run_trunk("push")
                self.assertEqual(rc, 0)
                output = out.getvalue()
                self.assertIn("Nothing to push", output)
                self.assertIn("gitshim", output)
            finally:
                os.chdir(cwd)

    async def test_cli_push_prints_summary_when_refs_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                repo = Repository.init(tmp, name="active-repo")
                remote_path = str(Path(tmp) / "remote.trunk")
                repo.set_backend_url(remote_path)
                backend = Local(Path(remote_path))
                async with backend:
                    engine = Engine(repo, backend)
                    await engine.write("README.md", b"# active\n")
                    await engine.commit(message="init")
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await run_trunk("push")
                self.assertEqual(rc, 0)
                output = out.getvalue()
                self.assertIn("Pushed", output)
                self.assertIn("ref", output)
                self.assertIn("object", output)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
