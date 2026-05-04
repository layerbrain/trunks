from __future__ import annotations

import os
import asyncio
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trunks.actions.workflow import list_workflow_runs
from trunks.actions.executor import execute_once_async
from trunks.repository import Repository
from trunks.storage import Storage


def _real_git() -> str:
    for entry in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if Path(entry).exists():
            return entry
    found = shutil.which("git")
    if not found:
        raise RuntimeError("git not on PATH")
    return found


GIT = _real_git()
TRUNKS_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _git_no_cwd(*args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _make_repo(root: Path, *, files: dict[str, bytes], message: str = "init") -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--initial-branch=main", "-q")
    _git(root, "config", "user.email", "test@trunks.local")
    _git(root, "config", "user.name", "Trunks Test")
    _git(root, "config", "commit.gpgsign", "false")
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message, "-q")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _setup_helper_env(tmp: Path, db_path: Path, mirror_db_path: Path | None = None) -> tuple[dict[str, str], Path]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git-remote-trunks"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -m trunks.remote_helper "$@"\n'
    )
    wrapper.chmod(0o755)

    trunks_home = tmp / "trunks_home"
    trunks_home.mkdir()
    config = trunks_home / "config"
    config_text = (
        "[storage.test]\n"
        "backend = url\n"
        "role = primary\n"
        f"setting.url = sqlite://{db_path}#{{repo}}\n"
    )
    if mirror_db_path is not None:
        config_text += (
            "\n[storage.backup]\n"
            "backend = url\n"
            "role = mirror\n"
            f"setting.url = sqlite://{mirror_db_path}#{{repo}}\n"
        )
    config.write_text(config_text)
    config.chmod(0o600)

    env = os.environ.copy()
    env.pop("TRUNKS_GIT_SHIM_DIR", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TRUNKS_HOME"] = str(trunks_home)
    env["TRUNKS_REAL_GIT"] = GIT
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{TRUNKS_REPO_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(TRUNKS_REPO_ROOT)
    return env, bin_dir


def _sqlite_ref(db_path: Path, trunk: str, ref: str) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select oid from trunks_refs where trunk = ? and name = ?",
            (trunk, ref),
        ).fetchone()
    return row[0] if row else None


class RealGitRoundTripTest(unittest.TestCase):
    def test_push_then_clone_through_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "storage.db"
            env, _bin_dir = _setup_helper_env(tmp_path, db_path)

            src = tmp_path / "src"
            sha = _make_repo(src, files={"a.txt": b"hello\n", "dir/b.txt": b"nested\n"})
            _git(src, "remote", "add", "origin", "trunks://test/myrepo")

            push = _git(src, "push", "-u", "origin", "main", env=env, check=False)
            self.assertEqual(push.returncode, 0, f"push failed: stderr={push.stderr} stdout={push.stdout}")

            dst = tmp_path / "dst"
            dst.mkdir()
            _git(dst, "init", "--initial-branch=main", "-q")
            _git(dst, "remote", "add", "origin", "trunks://test/myrepo")
            fetch = _git(dst, "fetch", "origin", env=env, check=False)
            self.assertEqual(fetch.returncode, 0, f"fetch failed: stderr={fetch.stderr} stdout={fetch.stdout}")

            commit_type = _git(dst, "cat-file", "-t", sha)
            self.assertEqual(commit_type.stdout.strip(), "commit")
            ls = _git(dst, "ls-tree", "-r", sha)
            self.assertIn("a.txt", ls.stdout)
            self.assertIn("dir/b.txt", ls.stdout)

    def test_git_clone_through_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "storage.db"
            env, _ = _setup_helper_env(tmp_path, db_path)

            src = tmp_path / "src"
            sha = _make_repo(src, files={"a.txt": b"hello\n", "dir/b.txt": b"nested\n"})
            _git(src, "remote", "add", "origin", "trunks://test/myrepo")
            push = _git(src, "push", "-u", "origin", "main", env=env, check=False)
            self.assertEqual(push.returncode, 0, f"push failed: {push.stderr}")

            clone_dir = tmp_path / "clone"
            clone = _git_no_cwd("clone", "trunks://test/myrepo", str(clone_dir), env=env, check=False)
            self.assertEqual(clone.returncode, 0, f"clone failed: stderr={clone.stderr} stdout={clone.stdout}")
            self.assertEqual(_git(clone_dir, "rev-parse", "HEAD").stdout.strip(), sha)
            self.assertEqual((clone_dir / "a.txt").read_text(encoding="utf-8"), "hello\n")

    def test_second_push_uploads_only_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "storage.db"
            env, _ = _setup_helper_env(tmp_path, db_path)

            src = tmp_path / "src"
            _make_repo(src, files={"a.txt": b"hi\n"})
            _git(src, "remote", "add", "origin", "trunks://test/myrepo")
            first = _git(src, "push", "-u", "origin", "main", env=env, check=False)
            self.assertEqual(first.returncode, 0, f"first push failed: {first.stderr}")

            (src / "a.txt").write_bytes(b"hi again\n")
            _git(src, "add", "-A")
            _git(src, "commit", "-m", "update", "-q")
            second = _git(src, "push", "origin", "main", env=env, check=False)
            self.assertEqual(second.returncode, 0, f"second push failed: {second.stderr}")

            dst = tmp_path / "dst"
            dst.mkdir()
            _git(dst, "init", "--initial-branch=main", "-q")
            _git(dst, "remote", "add", "origin", "trunks://test/myrepo")
            fetch = _git(dst, "fetch", "origin", env=env, check=False)
            self.assertEqual(fetch.returncode, 0, f"fetch failed: {fetch.stderr}")
            log = _git(dst, "log", "--oneline", "FETCH_HEAD")
            self.assertEqual(len(log.stdout.strip().splitlines()), 2)

    def test_named_primary_pushes_to_configured_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            primary_db = tmp_path / "primary.db"
            mirror_db = tmp_path / "mirror.db"
            env, _ = _setup_helper_env(tmp_path, primary_db, mirror_db)

            src = tmp_path / "src"
            sha = _make_repo(src, files={"a.txt": b"mirrored\n"})
            _git(src, "remote", "add", "origin", "trunks://test/myrepo")
            push = _git(src, "push", "-u", "origin", "main", env=env, check=False)
            self.assertEqual(push.returncode, 0, f"push failed: {push.stderr}")

            self.assertEqual(_sqlite_ref(primary_db, "myrepo", "refs/heads/main"), sha)
            self.assertEqual(_sqlite_ref(mirror_db, "myrepo", "refs/heads/main"), sha)
            trigger_prefix = f"refs/actions/repos/myrepo/triggers/push/{sha}"
            self.assertIsNotNone(_sqlite_ref(primary_db, "myrepo", trigger_prefix))
            self.assertIsNotNone(_sqlite_ref(mirror_db, "myrepo", trigger_prefix))

    def test_git_push_queues_local_workflow_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "storage.db"
            env, _ = _setup_helper_env(tmp_path, db_path)
            env["TRUNKS_ACTIONS_DISABLE_AUTO_EXECUTOR"] = "1"

            src = tmp_path / "src"
            sha = _make_repo(
                src,
                files={
                    "README.md": b"hello\n",
                    ".trunks/workflows/ci.yml": b"name: CI\non: [push]\njobs:\n  build:\n    steps:\n      - run: printf pushed\n",
                },
            )
            repo = Repository.init(src, name="myrepo")
            repo.set_storage_profile(Storage.from_url(name="test", role="primary", url=f"sqlite://{db_path}#{{repo}}"))
            _git(src, "remote", "add", "origin", "trunks://test/myrepo")

            push = _git(src, "push", "-u", "origin", "main", env=env, check=False)

            self.assertEqual(push.returncode, 0, f"push failed: {push.stderr}")
            runs = list_workflow_runs(repo)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["phase"], "pending")
            self.assertEqual(runs[0]["commit"], sha)
            result = asyncio.run(execute_once_async(repo, executor="test-executor", cwd=str(src)))
            self.assertIsNotNone(result)
            runs = list_workflow_runs(repo)
            self.assertEqual(runs[0]["phase"], "succeeded")
            self.assertEqual(runs[0]["jobs"][0]["phase"], "succeeded")

    def test_non_fast_forward_push_is_rejected_by_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "storage.db"
            env, _ = _setup_helper_env(tmp_path, db_path)

            a = tmp_path / "a"
            _make_repo(a, files={"a.txt": b"a\n"})
            _git(a, "remote", "add", "origin", "trunks://test/myrepo")
            self.assertEqual(_git(a, "push", "-u", "origin", "main", env=env, check=False).returncode, 0)

            b = tmp_path / "b"
            _make_repo(b, files={"b.txt": b"b\n"})
            _git(b, "remote", "add", "origin", "trunks://test/myrepo")
            push = _git(b, "push", "origin", "main", env=env, check=False)
            self.assertNotEqual(push.returncode, 0)
            combined = (push.stderr + push.stdout).lower()
            self.assertTrue(
                "non-fast-forward" in combined or "rejected" in combined,
                f"expected non-fast-forward rejection, got: {push.stderr} / {push.stdout}",
            )


if __name__ == "__main__":
    unittest.main()
