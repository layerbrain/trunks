from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trunks.errors import InvalidPath, RepositoryCorrupt
from trunks.objects import Blob, Commit, Tree, TreeEntry
from trunks.paths import normalize_path
from trunks.repository import Repository
from trunks.session import ChangedFile

TRUNKS_SRC = str(Path(__file__).resolve().parents[2])


def run_gitshim(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": TRUNKS_SRC, "TRUNKS_ACTIVE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "trunks.gitshim", *args],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


class PathAndIntegrityTests(unittest.TestCase):
    def test_normalize_path_rejects_nul_byte(self) -> None:
        with self.assertRaises(InvalidPath):
            normalize_path("a\x00b")

    def test_normalize_path_rejects_newline(self) -> None:
        with self.assertRaises(InvalidPath):
            normalize_path("a\nb")

    def test_normalize_path_rejects_all_control_chars(self) -> None:
        for ch in ("\x00", "\n", "\r", "\t", "\x01", "\x1f", "\x7f"):
            with self.assertRaises(InvalidPath, msg=f"accepted ord={ord(ch)}"):
                normalize_path(f"a{ch}b")

    def test_corrupt_db_status_returns_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Repository.init(root)
            db = next((root / ".trunks").glob("*.trunk"))
            db.write_bytes(b"garbage" * 100)

            env = {**os.environ, "PYTHONPATH": TRUNKS_SRC}
            result = subprocess.run(
                [sys.executable, "-m", "trunks.cli", "status"],
                cwd=root,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("corrupt repository database", result.stderr)

    def test_corrupt_db_check_returns_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Repository.init(root)
            db = next((root / ".trunks").glob("*.trunk"))
            db.write_bytes(b"garbage" * 100)

            env = {**os.environ, "PYTHONPATH": TRUNKS_SRC}
            result = subprocess.run(
                [sys.executable, "-m", "trunks.cli", "check"],
                cwd=root,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("corrupt repository database", result.stderr)

    def test_gitignore_applies_at_git_add_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root)
            (root / ".gitignore").write_text("ignored.txt\nnode_modules/\n!keep.txt\n", encoding="utf-8")
            (root / "ignored.txt").write_text("ignored", encoding="utf-8")
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            (root / "node_modules" / "pkg").mkdir(parents=True)
            (root / "node_modules" / "pkg" / "index.js").write_text("pkg", encoding="utf-8")

            repo.add_all_worktree_files()

            self.assertEqual([entry.path for entry in repo.index_entries()], [".gitignore", "keep.txt"])

    def test_gitignore_is_slash_aware_and_supports_negation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root)
            (root / ".gitignore").write_text(
                "*.log\n"
                "docs/*.tmp\n"
                "secrets/\n"
                "!secrets/keep.txt\n",
                encoding="utf-8",
            )
            (root / "app.log").write_text("ignored", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "scratch.tmp").write_text("ignored", encoding="utf-8")
            (root / "docs" / "nested").mkdir()
            (root / "docs" / "nested" / "scratch.tmp").write_text("tracked", encoding="utf-8")
            (root / "secrets").mkdir()
            (root / "secrets" / "drop.txt").write_text("ignored", encoding="utf-8")
            (root / "secrets" / "keep.txt").write_text("tracked", encoding="utf-8")

            repo.add_all_worktree_files()

            self.assertEqual(
                [entry.path for entry in repo.index_entries()],
                [".gitignore", "docs/nested/scratch.tmp", "secrets/keep.txt"],
            )

    def test_git_add_force_overrides_gitignore_but_never_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root)
            db = next((root / ".trunks").glob("*.trunk"))
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "ignored.txt").write_text("ignored", encoding="utf-8")

            result = run_gitshim(root, "add", "ignored.txt")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(repo.index_entries(), [])

            result = run_gitshim(root, "add", "-f", "ignored.txt")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([entry.path for entry in repo.index_entries()], ["ignored.txt"])

            result = run_gitshim(root, "add", "-f", str(db))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([entry.path for entry in repo.index_entries()], ["ignored.txt"])

    def test_git_add_dash_dash_accepts_path_named_like_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root)
            (root / "-looks-like-option").write_text("tracked", encoding="utf-8")

            result = run_gitshim(root, "add", "--", "-looks-like-option")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([entry.path for entry in repo.index_entries()], ["-looks-like-option"])

    def test_direct_api_write_does_not_apply_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root)
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")

            repo.write_file("ignored.txt", b"direct api is explicit")

            self.assertEqual([entry.path for entry in repo.index_entries()], ["ignored.txt"])

    def test_direct_api_cannot_track_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)

            with self.assertRaises(InvalidPath):
                repo.write_file(".trunks/db.trunk", b"nope")
            with self.assertRaises(InvalidPath):
                repo.write_file(".git/config", b"nope")

            self.assertEqual(repo.index_entries(), [])

    def test_session_capture_cannot_track_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)

            with self.assertRaises(InvalidPath):
                repo.commit_session_changes(
                    branch="main",
                    changes=[ChangedFile(".git/config", b"nope")],
                    message="nope",
                )

            self.assertEqual(repo.index_entries(), [])

    def test_symlinked_file_is_not_ingested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.NamedTemporaryFile() as outside:
            root = Path(tmp)
            repo = Repository.init(root)
            outside.write(b"secret")
            outside.flush()
            (root / "leak.txt").symlink_to(outside.name)

            repo.add_all_worktree_files()

            self.assertEqual(repo.index_entries(), [])

    def test_external_git_add_path_is_rejected_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.NamedTemporaryFile() as outside:
            root = Path(tmp)
            Repository.init(root)
            outside.write(b"outside")
            outside.flush()

            result = run_gitshim(root, "add", outside.name)

            self.assertEqual(result.returncode, 128)
            self.assertIn("fatal:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_git_rm_outside_path_does_not_unlink_outside_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Repository.init(root)
            outside = root.parent / f"{root.name}-outside-rm.txt"
            outside.write_text("keep", encoding="utf-8")
            try:
                result = run_gitshim(root, "rm", str(outside))

                self.assertEqual(result.returncode, 128)
                self.assertTrue(outside.exists())
                self.assertNotIn("Traceback", result.stderr)
            finally:
                outside.unlink(missing_ok=True)

    def test_git_mv_outside_path_does_not_move_outside_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Repository.init(root)
            outside = root.parent / f"{root.name}-outside-mv.txt"
            dest = root / "stolen.txt"
            outside.write_text("keep", encoding="utf-8")
            try:
                result = run_gitshim(root, "mv", str(outside), "stolen.txt")

                self.assertEqual(result.returncode, 128)
                self.assertTrue(outside.exists())
                self.assertFalse(dest.exists())
                self.assertNotIn("Traceback", result.stderr)
            finally:
                outside.unlink(missing_ok=True)

    def test_git_restore_outside_path_does_not_unlink_outside_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Repository.init(root)
            outside = root.parent / f"{root.name}-outside-restore.txt"
            outside.write_text("keep", encoding="utf-8")
            try:
                result = run_gitshim(root, "restore", str(outside))

                self.assertEqual(result.returncode, 128)
                self.assertTrue(outside.exists())
                self.assertNotIn("Traceback", result.stderr)
            finally:
                outside.unlink(missing_ok=True)

    def test_git_diff_outside_path_does_not_read_outside_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Repository.init(root)
            outside = root.parent / f"{root.name}-outside-diff.txt"
            outside.write_text("secret-token", encoding="utf-8")
            try:
                result = run_gitshim(root, "diff", str(outside))

                self.assertEqual(result.returncode, 128)
                self.assertNotIn("secret-token", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
            finally:
                outside.unlink(missing_ok=True)

    def test_repository_external_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.NamedTemporaryFile() as outside:
            repo = Repository.init(tmp)
            with self.assertRaises(InvalidPath):
                repo.add_worktree_path(Path(outside.name))

    def test_check_reports_invalid_ref_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            repo.write_file("a.txt", b"a")
            repo.create_commit(message="init")
            with repo._connect() as conn:
                conn.execute("update refs set oid = ? where name = ?", ("not-a-sha", "refs/heads/main"))

            errors = repo.check_integrity()

            self.assertEqual(errors, ["invalid ref target: refs/heads/main -> not-a-sha"])

    def test_check_reports_corrupt_object_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            repo.write_file("a.txt", b"a")
            repo.create_commit(message="init")
            with repo._connect() as conn:
                row = conn.execute("select oid from objects where kind = 'blob'").fetchone()
                conn.execute("update objects set data = ? where oid = ?", (b"bad", row["oid"]))

            errors = repo.check_integrity()

            self.assertEqual(len(errors), 1)
            self.assertIn("corrupt object:", errors[0])

    def test_check_reports_missing_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            repo.write_file("a.txt", b"a")
            repo.create_commit(message="init")
            with repo._connect() as conn:
                row = conn.execute("select oid from objects where kind = 'blob'").fetchone()
                conn.execute("delete from objects where oid = ?", (row["oid"],))

            errors = repo.check_integrity()

            self.assertEqual(len(errors), 1)
            self.assertIn("missing object:", errors[0])

    def test_clean_refuses_corrupt_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            repo.write_file("a.txt", b"a")
            repo.create_commit(message="init")
            with repo._connect() as conn:
                row = conn.execute("select oid from objects where kind = 'blob'").fetchone()
                conn.execute("update objects set data = ? where oid = ?", (b"bad", row["oid"]))

            with self.assertRaises(RepositoryCorrupt):
                repo.clean_unreachable()

    def test_checkout_rejects_malicious_tree_path_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root)
            blob = Blob.from_data(b"owned")
            repo.put_object(blob.id, blob.canonical())
            tree = Tree.from_entries([TreeEntry("100644", "../owned", blob.id)])
            repo.put_object(tree.id, tree.canonical())
            commit = Commit.create(
                tree=tree.id,
                parents=[],
                author=repo.git_identity(),
                committer=repo.git_identity(),
                message="malicious",
            )
            repo.put_object(commit.id, commit.canonical())
            repo.set_ref("main", commit.id)
            outside = root.parent / "owned"

            with self.assertRaises(InvalidPath):
                repo.checkout_tree(commit.id)

            self.assertTrue(root.exists())
            self.assertFalse(outside.exists())

    def test_check_reports_malicious_tree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(root)
            blob = Blob.from_data(b"owned")
            repo.put_object(blob.id, blob.canonical())
            tree = Tree.from_entries([TreeEntry("100644", ".trunks", blob.id)])
            repo.put_object(tree.id, tree.canonical())
            commit = Commit.create(
                tree=tree.id,
                parents=[],
                author=repo.git_identity(),
                committer=repo.git_identity(),
                message="malicious",
            )
            repo.put_object(commit.id, commit.canonical())
            repo.set_ref("main", commit.id)

            errors = repo.check_integrity()

            self.assertEqual(len(errors), 1)
            self.assertIn("corrupt tree object:", errors[0])


if __name__ == "__main__":
    unittest.main()
