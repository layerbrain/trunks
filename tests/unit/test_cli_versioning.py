from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch
from trunks.repository import Repository


class CliVersioningTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_is_idempotent_and_does_not_require_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                (root / "README.md").write_text("# hi\n", encoding="utf-8")
                with patch.dict(os.environ, {"PATH": ""}, clear=False), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init again"]), 0)
                repo = Repository.find(root)
                self.assertIsNotNone(repo.ref("main"))
                self.assertIn("No changes to checkpoint", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_branch_subcommands_create_switch_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                    self.assertEqual(await dispatch(["branch", "create", "--name", "feature/auth", "--from", "main"]), 0)
                    self.assertEqual(await dispatch(["branch", "switch", "--name", "feature/auth"]), 0)
                    self.assertEqual(await dispatch(["branch", "create", "--name", "feature/cleanup", "--from", "main"]), 0)
                    self.assertEqual(await dispatch(["branch", "delete", "--name", "feature/cleanup"]), 0)
                repo = Repository.find(root)
                self.assertEqual(repo.current_branch, "feature/auth")
                self.assertIsNotNone(repo.ref("feature/auth"))
                self.assertIsNone(repo.ref("feature/cleanup"))
            finally:
                os.chdir(cwd)

    async def test_branch_list_json_uses_api_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                    self.assertEqual(await dispatch(["branch", "create", "--name", "feature/auth", "--from", "main"]), 0)
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["branch", "list", "--json", "--limit", "1"]), 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["object"], "list")
                self.assertEqual(payload["limit"], 1)
                self.assertEqual(payload["offset"], 0)
                self.assertEqual(payload["total_count"], 2)
                self.assertTrue(payload["has_more"])
                self.assertEqual(len(payload["data"]), 1)
                self.assertEqual(payload["data"][0]["object"], "branch")
            finally:
                os.chdir(cwd)

    async def test_tag_subcommands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                    self.assertEqual(await dispatch(["tag", "create", "--name", "v1.0", "--at", "main"]), 0)
                repo = Repository.find(root)
                self.assertIsNotNone(repo.ref("refs/tags/v1.0"))
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["tag", "list"]), 0)
                self.assertIn("v1.0", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_tag_list_json_uses_api_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                    self.assertEqual(await dispatch(["tag", "create", "--name", "v1.0", "--at", "main"]), 0)
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["tag", "list", "--json"]), 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["object"], "list")
                self.assertEqual(payload["data"][0]["object"], "tag")
                self.assertEqual(payload["data"][0]["name"], "v1.0")
                self.assertFalse(payload["has_more"])
            finally:
                os.chdir(cwd)

    async def test_repo_list_json_uses_api_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["repo", "list", "--json"]), 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["object"], "list")
                self.assertEqual(payload["data"][0]["object"], "repo")
                self.assertEqual(payload["data"][0]["name"], "demo")
                self.assertEqual(payload["data"][0]["current_branch"], "main")
            finally:
                os.chdir(cwd)

    async def test_diff_reports_worktree_changes_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                (root / "README.md").write_text("# bye\n", encoding="utf-8")
                (root / "new.txt").write_text("new\n", encoding="utf-8")
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["diff", "--vs", "main", "--json"]), 0)
                self.assertEqual(
                    json.loads(out.getvalue()),
                    [{"path": "README.md", "status": "M"}, {"path": "new.txt", "status": "A"}],
                )
            finally:
                os.chdir(cwd)

    async def test_rollback_moves_current_branch_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("one\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "one"]), 0)
                    self.assertEqual(await dispatch(["tag", "create", "--name", "v1", "--at", "main"]), 0)
                    (root / "README.md").write_text("two\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "two"]), 0)
                    self.assertEqual(await dispatch(["rollback", "--to", "tags/v1"]), 0)
                self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "one\n")
                repo = Repository.find(root)
                self.assertEqual(repo.ref("main"), repo.ref("refs/tags/v1"))
            finally:
                os.chdir(cwd)

    async def test_history_json_contains_refs_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("one\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "one"]), 0)
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["history", "--json"]), 0)
                payload = json.loads(out.getvalue())
                self.assertIn("refs/heads/main", payload["refs"])
                self.assertIn(payload["refs"]["refs/heads/main"], payload["commits"])
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
