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


class StatusWorktreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_untracked_files_make_status_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                (root / "a.md").write_text("a\n", encoding="utf-8")
                (root / "b.md").write_text("b\n", encoding="utf-8")
                repo = Repository.find(root)
                state = repo.status(compare_worktree=True)
                self.assertEqual(state.untracked, ("a.md", "b.md"))
                self.assertEqual(state.modified, ())
                self.assertEqual(state.deleted, ())
                self.assertTrue(state.dirty)
            finally:
                os.chdir(cwd)

    async def test_modified_and_deleted_paths_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "keep.md").write_text("keep\n", encoding="utf-8")
                    (root / "drop.md").write_text("drop\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                # Modify one indexed file, delete another
                (root / "keep.md").write_text("keep-changed\n", encoding="utf-8")
                (root / "drop.md").unlink()
                repo = Repository.find(root)
                state = repo.status(compare_worktree=True)
                self.assertEqual(state.modified, ("keep.md",))
                self.assertEqual(state.deleted, ("drop.md",))
                self.assertEqual(state.untracked, ())
                self.assertTrue(state.dirty)
            finally:
                os.chdir(cwd)

    async def test_ignored_and_internal_paths_never_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                # Things that must never appear: .trunks/* (internal), .git/*,
                # node_modules/* (default ignore), __pycache__/*.
                (root / "node_modules").mkdir()
                (root / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")
                (root / "__pycache__").mkdir()
                (root / "__pycache__" / "x.pyc").write_text("x\n", encoding="utf-8")
                (root / ".git").mkdir()
                (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
                # Tracked file so the run is meaningful
                (root / "real.md").write_text("real\n", encoding="utf-8")
                repo = Repository.find(root)
                state = repo.status(compare_worktree=True)
                self.assertEqual(state.untracked, ("real.md",))
                for noise in (".git/HEAD", "node_modules/pkg.js", "__pycache__/x.pyc"):
                    self.assertNotIn(noise, state.untracked)
                    self.assertNotIn(noise, state.modified)
                    self.assertNotIn(noise, state.deleted)
            finally:
                os.chdir(cwd)

    async def test_clean_repo_reports_clean(self) -> None:
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
                state = repo.status(compare_worktree=True)
                self.assertFalse(state.dirty)
                self.assertEqual(state.untracked, ())
                self.assertEqual(state.modified, ())
                self.assertEqual(state.deleted, ())
            finally:
                os.chdir(cwd)

    async def test_status_json_emits_worktree_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                (root / "a.md").write_text("a\n", encoding="utf-8")
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["status", "--json"]), 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["untracked"], ["a.md"])
                self.assertEqual(payload["modified"], [])
                self.assertEqual(payload["deleted"], [])
                self.assertTrue(payload["dirty"])
            finally:
                os.chdir(cwd)

    async def test_stat_cache_avoids_rehash_on_unchanged_files(self) -> None:
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
                # First call populates cache. Second call should hit fast path
                # for every file — verify by patching Blob.from_data and asserting
                # it's called zero times the second time around.
                repo.status(compare_worktree=True)
                cache_path = repo._status_stat_cache_path()
                self.assertTrue(cache_path.exists())
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertIn("a.md", cached)
                self.assertIn("oid", cached["a.md"])

                with patch("trunks.repository.Blob.from_data") as fake:
                    repo.status(compare_worktree=True)
                self.assertEqual(fake.call_count, 0)
            finally:
                os.chdir(cwd)

    async def test_stat_cache_rehashes_when_mtime_changes(self) -> None:
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
                repo.status(compare_worktree=True)
                # Bump mtime so the fast-path stat compare misses
                target = root / "a.md"
                old_stat = target.stat()
                os.utime(target, (old_stat.st_atime, old_stat.st_mtime + 1))
                with patch("trunks.repository.Blob.from_data", wraps=__import__("trunks.repository", fromlist=["Blob"]).Blob.from_data) as wrapped:
                    state = repo.status(compare_worktree=True)
                self.assertGreaterEqual(wrapped.call_count, 1)
                # Content unchanged → still clean
                self.assertFalse(state.dirty)
            finally:
                os.chdir(cwd)

    async def test_stat_cache_rehashes_when_same_size_content_changes_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "a.md").write_text("aaaa\n", encoding="utf-8")
                    self.assertEqual(await dispatch(["checkpoint", "-m", "init"]), 0)
                repo = Repository.find(root)
                repo.status(compare_worktree=True)
                cache = json.loads(repo._status_stat_cache_path().read_text(encoding="utf-8"))
                cached_mtime_ns = int(cache["a.md"]["mtime_ns"])

                target = root / "a.md"
                target.write_text("bbbb\n", encoding="utf-8")
                os.utime(target, ns=(cached_mtime_ns, cached_mtime_ns))

                state = repo.status(compare_worktree=True)
                self.assertEqual(state.modified, ("a.md",))
                self.assertTrue(state.dirty)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
