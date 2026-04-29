from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch
from trunks.repository import Repository


class CliMountDoctorTests(unittest.IsolatedAsyncioTestCase):
    async def test_mount_creates_repo_at_named_path_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-app"
            with patch.dict(os.environ, {"PATH": ""}, clear=False):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await dispatch(["mount", "--repo", "my-app", "--path", str(target)])
            self.assertEqual(rc, 0)
            self.assertEqual(Repository.find(target).name, "my-app")
            self.assertIn("Mounted Trunks repository", out.getvalue())
            self.assertIn("none (local-only)", out.getvalue())

    async def test_mount_require_existing_fails_without_creating_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "missing"
            err = io.StringIO()
            with redirect_stderr(err):
                rc = await dispatch(["mount", "--repo", "my-app", "--path", str(target), "--require-existing"])
            self.assertEqual(rc, 1)
            self.assertFalse((target / ".trunks").exists())
            self.assertIn("no Trunks repo", err.getvalue())

    async def test_status_reads_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-app"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(await dispatch(["mount", "--repo", "my-app", "--path", str(target)]), 0)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = await dispatch(["status", "--path", str(target), "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["repository"], "my-app")
            self.assertEqual(payload["path"], str(target.resolve()))

    async def test_doctor_reports_missing_git_without_failing_local_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-app"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(await dispatch(["mount", "--repo", "my-app", "--path", str(target)]), 0)
            out = io.StringIO()
            with patch.dict(os.environ, {"PATH": ""}, clear=False), redirect_stdout(out):
                rc = await dispatch(["doctor", "--path", str(target), "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["ok"])
            git = next(check for check in payload["checks"] if check["name"] == "git")
            self.assertFalse(git["ok"])

    async def test_doctor_json_ping_emits_only_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my-app"
            remote = Path(tmp) / "remote"
            cwd = Path.cwd()
            os.chdir(target.parent)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["mount", "--repo", "my-app", "--path", str(target)]), 0)
                os.chdir(target)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["storage", "add", "--name", "primary", "--backend", "local", "--path", str(remote)]), 0)
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await dispatch(["doctor", "--path", str(target), "--ping", "--json"])
                self.assertEqual(rc, 0)
                payload = json.loads(out.getvalue())
                self.assertTrue(payload["ok"])
                ping = next(check for check in payload["checks"] if check["name"] == "storage_ping")
                self.assertTrue(ping["ok"])
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
