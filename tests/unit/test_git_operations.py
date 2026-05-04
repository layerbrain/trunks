from __future__ import annotations

import os
import tempfile
import unittest
import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from trunks.cli import dispatch
from trunks.gitshim import main as git_main
from trunks.repository import Repository


@contextmanager
def cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    old_env = {
        "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME"),
        "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL"),
        "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME"),
        "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL"),
        "TRUNKS_ACTIVE": os.environ.get("TRUNKS_ACTIVE"),
    }
    os.environ.update(
        {
            "GIT_AUTHOR_NAME": "Brain",
            "GIT_AUTHOR_EMAIL": "brain@layerbrain.com",
            "GIT_COMMITTER_NAME": "Brain",
            "GIT_COMMITTER_EMAIL": "brain@layerbrain.com",
            "TRUNKS_ACTIVE": "1",
        }
    )
    try:
        yield
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.chdir(previous)


def git(*args: str) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return git_main(list(args))


class GitOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_git_workflow_operations_use_one_trunks_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with cwd(root):
                self.assertEqual(await dispatch(["init"]), 0)

                (root / "README.md").write_text("base\n", encoding="utf-8")
                self.assertEqual(git("add", "."), 0)
                self.assertEqual(git("commit", "-m", "initial"), 0)

                self.assertEqual(git("branch", "feature/auth"), 0)
                self.assertEqual(git("checkout", "feature/auth"), 0)
                (root / "README.md").write_text("base\nauth\n", encoding="utf-8")
                self.assertEqual(git("add", "README.md"), 0)
                self.assertEqual(git("commit", "-m", "auth"), 0)
                feature_commit = Repository.find(root).ref("feature/auth")
                self.assertIsNotNone(feature_commit)

                self.assertEqual(git("checkout", "main"), 0)
                self.assertEqual(git("merge", "feature/auth"), 0)
                repo = Repository.find(root)
                self.assertEqual(repo.ref("main"), feature_commit)
                self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "base\nauth\n")

                (root / "README.md").write_text("base\nauth\nscratch\n", encoding="utf-8")
                with redirect_stdout(StringIO()) as diff_out:
                    self.assertEqual(git_main(["diff"]), 0)
                self.assertIn("+scratch", diff_out.getvalue())
                self.assertEqual(git("restore", "README.md"), 0)
                self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "base\nauth\n")

                self.assertEqual(git("tag", "v1.0.0"), 0)
                with redirect_stdout(StringIO()) as tags:
                    self.assertEqual(git_main(["tag"]), 0)
                self.assertIn("v1.0.0", tags.getvalue())
                with redirect_stdout(StringIO()) as shown:
                    self.assertEqual(git_main(["show", "HEAD"]), 0)
                self.assertIn("commit ", shown.getvalue())

                self.assertEqual(git("checkout", "-b", "feature/files"), 0)
                (root / "src").mkdir()
                (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
                self.assertEqual(git("add", "src/app.py"), 0)
                self.assertEqual(git("commit", "-m", "add app"), 0)
                self.assertEqual(git("mv", "src/app.py", "src/main.py"), 0)
                self.assertEqual(git("commit", "-m", "move app"), 0)
                self.assertEqual(git("rm", "src/main.py"), 0)
                self.assertEqual(git("commit", "-m", "remove app"), 0)
                with redirect_stdout(StringIO()) as files:
                    self.assertEqual(git_main(["ls-files"]), 0)
                self.assertNotIn("src/main.py", files.getvalue())

                self.assertEqual(git("checkout", "main"), 0)
                (root / "other.txt").write_text("main\n", encoding="utf-8")
                self.assertEqual(git("add", "other.txt"), 0)
                self.assertEqual(git("commit", "-m", "main change"), 0)
                self.assertEqual(git("checkout", "feature/files"), 0)
                self.assertEqual(git("rebase", "main"), 0)
                self.assertEqual(git("checkout", "main"), 0)
                self.assertEqual(git("checkout", "-b", "feature/pick"), 0)
                (root / "picked.txt").write_text("picked\n", encoding="utf-8")
                self.assertEqual(git("add", "picked.txt"), 0)
                self.assertEqual(git("commit", "-m", "picked"), 0)
                picked_tip = Repository.find(root).ref("feature/pick")
                self.assertIsNotNone(picked_tip)
                self.assertEqual(git("checkout", "main"), 0)
                self.assertEqual(git("cherry-pick", str(picked_tip)), 0)
                self.assertEqual((root / "picked.txt").read_text(encoding="utf-8"), "picked\n")

                self.assertEqual(git("remote", "add", "origin", "git@github.com:acme/lazy-lms.git"), 0)
                with redirect_stdout(StringIO()) as remote:
                    self.assertEqual(git_main(["remote", "-v"]), 0)
                self.assertIn("lazy-lms.git", remote.getvalue())
                self.assertEqual(git("remote", "remove", "origin"), 0)

                self.assertEqual(git("branch", "-d", "feature/auth"), 0)
                self.assertNotEqual(git("worktree"), 0)


if __name__ == "__main__":
    unittest.main()
