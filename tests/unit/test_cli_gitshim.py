from __future__ import annotations

import os
import asyncio
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch, shim
from trunks.actions.artifacts import get_artifact
from trunks.actions.storage import load_run
from trunks.actions.workflow import list_workflow_runs
from trunks.gitshim import main as git_main
from trunks.repository import Repository


@contextmanager
def cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class CliGitShimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_init_status_and_git_commit_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with cwd(root):
                with redirect_stdout(StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(git_main(["add", "."]), 0)
                    self.assertEqual(git_main(["commit", "-m", "init"]), 0)
                    repo = Repository.find(root)
                    self.assertIsNotNone(repo.ref("main"))
                    self.assertEqual(git_main(["checkout", "-b", "feature/auth"]), 0)
                    self.assertEqual(repo.current_branch, "feature/auth")
                    self.assertEqual(git_main(["status"]), 0)
                    self.assertEqual(Repository.find(root).current_branch, "feature/auth")

    async def test_git_push_without_origin_pushes_to_trunks_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp)
            remote_root = Path(remote) / "repo.trunk"
            with cwd(root):
                with redirect_stdout(StringIO()):
                    self.assertEqual(await dispatch(["init", "--backend", f"local://{remote_root}"]), 0)
                    self.assertEqual(git_main(["remote"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(git_main(["add", "."]), 0)
                    self.assertEqual(git_main(["commit", "-m", "init"]), 0)
                    self.assertEqual(git_main(["push"]), 0)
                self.assertTrue((remote_root / "refs" / "heads" / "main").exists())

    async def test_git_push_triggers_trunks_workflow_push_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp)
            remote_root = Path(remote) / "repo.trunk"
            with cwd(root):
                with redirect_stdout(StringIO()):
                    self.assertEqual(await dispatch(["init", "--backend", f"local://{remote_root}"]), 0)
                    workflows = root / ".trunks" / "workflows"
                    workflows.mkdir(parents=True)
                    (workflows / "ci.yml").write_text(
                        """
name: CI
on: [push]
jobs:
  test:
    steps:
      - run: printf git-push-triggered
""".strip(),
                        encoding="utf-8",
                    )
                    self.assertEqual(git_main(["add", "."]), 0)
                    self.assertEqual(git_main(["commit", "-m", "add ci"]), 0)
                    self.assertEqual(git_main(["push"]), 0)

                repo = Repository.find(root)
                workflow_runs = list_workflow_runs(repo)
                self.assertEqual(len(workflow_runs), 1)
                self.assertEqual(workflow_runs[0]["phase"], "succeeded")
                self.assertEqual(workflow_runs[0]["workflow"]["name"], "CI")

    async def test_installed_git_command_push_triggers_trunks_workflow_push_actions(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as remote,
            tempfile.TemporaryDirectory() as shim_dir,
            tempfile.TemporaryDirectory() as fake_home,
        ):
            root = Path(tmp)
            remote_root = Path(remote) / "repo.trunk"
            with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                with redirect_stdout(StringIO()):
                    self.assertEqual(shim("install", shim_dir), 0)
            Repository.init(cwd=root, name="repo", backend=f"local://{remote_root}")
            workflows = root / ".trunks" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "ci.yml").write_text(
                """
name: CI
on: [push]
jobs:
  test:
    trunks:
      spec:
        cpu: 1
        memory: 1
        disk: 1
    steps:
      - run: printf installed-git-triggered > provider-marker.txt
      - uses: actions/upload-artifact@v4
        with:
          path: provider-marker.txt
""".strip(),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": fake_home,
                "XDG_CONFIG_HOME": str(Path(fake_home) / ".config"),
                "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            for args in (["git", "add", "."], ["git", "commit", "-m", "add ci"], ["git", "push"]):
                result = subprocess.run(args, cwd=root, env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, msg=f"{args}: {result.stderr}\n{result.stdout}")

            repo = Repository.find(root)
            workflow_runs = list_workflow_runs(repo)
            self.assertEqual(len(workflow_runs), 1)
            self.assertEqual(workflow_runs[0]["phase"], "succeeded")
            self.assertEqual(workflow_runs[0]["workflow"]["name"], "CI")
            run_id = workflow_runs[0]["jobs"][0]["run"]
            job_run = load_run(repo, run_id)
            self.assertEqual(job_run["state"]["provider"], "local")
            self.assertEqual(job_run["result"]["exit_code"], 0)
            self.assertEqual(get_artifact(repo, run_id, "provider-marker.txt"), b"installed-git-triggered")

    async def test_git_branch_verbose_does_not_create_dash_vv_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with cwd(root):
                with redirect_stdout(StringIO()):
                    self.assertEqual(await dispatch(["init"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(git_main(["add", "."]), 0)
                    self.assertEqual(git_main(["commit", "-m", "init"]), 0)
                    self.assertEqual(git_main(["branch", "-vv"]), 0)
                repo = Repository.find(root)
                refs = dict(repo.list_refs())
                self.assertIn("refs/heads/main", refs)
                self.assertNotIn("refs/heads/-vv", refs)

    async def test_trunks_backend_is_visible_as_origin_for_read_only_git_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp)
            remote_root = Path(remote) / "repo.trunk"
            with cwd(root):
                with redirect_stdout(StringIO()):
                    self.assertEqual(await dispatch(["init", "--backend", f"local://{remote_root}"]), 0)
                    (root / "README.md").write_text("# hi\n", encoding="utf-8")
                    self.assertEqual(git_main(["add", "."]), 0)
                    self.assertEqual(git_main(["commit", "-m", "init"]), 0)
                    self.assertEqual(git_main(["push"]), 0)

                remote_out = StringIO()
                with redirect_stdout(remote_out):
                    self.assertEqual(git_main(["remote", "-v"]), 0)
                self.assertIn(f"origin\tlocal://{remote_root}", remote_out.getvalue())

                log_out = StringIO()
                with redirect_stdout(log_out):
                    self.assertEqual(git_main(["log", "origin/main", "-1", "--oneline"]), 0)
                self.assertTrue((root / ".git" / "refs" / "remotes" / "origin" / "main").exists())

    async def test_init_imports_existing_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/site.git"], cwd=root, check=True)
            (root / "README.md").write_text("# existing\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "existing"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

            with cwd(root), redirect_stdout(StringIO()):
                self.assertEqual(await dispatch(["init"]), 0)

            repo = Repository.find(root)
            self.assertEqual(str(repo.ref("main")), head)
            self.assertEqual(repo.get_meta("git_origin"), "https://github.com/example/site.git")
            self.assertEqual(repo.read_file("README.md"), b"# existing\n")

    async def test_managed_shell_requires_git_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with cwd(Path(tmp)), patch.dict(os.environ, {"PATH": ""}, clear=False):
                err = StringIO()
                with redirect_stderr(err):
                    rc = await dispatch([])
                self.assertEqual(rc, 1)
                self.assertIn("Git was not found.", err.getvalue())
                self.assertIn("xcode-select --install", err.getvalue())
                self.assertIn("sudo apt install git", err.getvalue())


if __name__ == "__main__":
    unittest.main()
