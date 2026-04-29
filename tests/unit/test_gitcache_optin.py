from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch
from trunks.gitcache import GitCache, _shim_marker_path
from trunks.repository import Repository


class GitCacheOptInTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_repo_without_shim_does_not_create_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(await dispatch(["init"]), 0)
                        (root / "a.md").write_text("a\n", encoding="utf-8")
                        self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                self.assertFalse((root / ".git").exists(), ".git/ should not be materialised without opt-in")
            finally:
                os.chdir(cwd)

    async def test_rollback_without_shim_does_not_create_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(await dispatch(["init"]), 0)
                        (root / "a.md").write_text("one\n", encoding="utf-8")
                        self.assertEqual(await dispatch(["checkpoint", "-m", "one"]), 0)
                        self.assertEqual(await dispatch(["tag", "create", "--name", "v1", "--at", "main"]), 0)
                        (root / "a.md").write_text("two\n", encoding="utf-8")
                        self.assertEqual(await dispatch(["checkpoint", "-m", "two"]), 0)
                        self.assertEqual(await dispatch(["rollback", "--to", "tags/v1"]), 0)
                self.assertFalse((root / ".git").exists())
            finally:
                os.chdir(cwd)

    async def test_existing_git_dir_keeps_being_maintained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(await dispatch(["init"]), 0)
                        (root / "a.md").write_text("a\n", encoding="utf-8")
                        self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                self.assertTrue((root / ".git" / "objects").exists(), "GitCache should populate .git/ when it pre-existed")
                self.assertTrue((root / ".git" / "config").exists())
            finally:
                os.chdir(cwd)

    async def test_shim_marker_enables_git_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                    # Plant marker as if `trunks shim install` ran.
                    marker = _shim_marker_path()
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text("1\n", encoding="utf-8")
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(await dispatch(["init"]), 0)
                        (root / "a.md").write_text("a\n", encoding="utf-8")
                        self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                self.assertTrue((root / ".git").exists(), "shim marker should opt-in to GitCache.rebuild()")
                self.assertTrue((root / ".git" / "objects").exists())
            finally:
                os.chdir(cwd)

    def test_gitcache_short_circuits_without_optin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                cwd = Path.cwd()
                os.chdir(root)
                try:
                    with redirect_stdout(io.StringIO()):
                        from trunks.cli import dispatch
                        import asyncio
                        asyncio.run(dispatch(["init"]))
                    repo = Repository.find(root)
                    cache = GitCache(repo)
                    self.assertFalse(cache._git_interop_active())
                    cache.rebuild()
                    self.assertFalse((root / ".git").exists())
                finally:
                    os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
