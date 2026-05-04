from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch


def _real_git() -> str | None:
    for entry in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if Path(entry).exists():
            return entry
    return shutil.which("git")


GIT = _real_git()


def _write_global_config(home: Path, body: str) -> None:
    config = home / "config"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(body)
    config.chmod(0o600)


def _git_config(target: Path, key: str) -> str:
    result = subprocess.run(
        [GIT, "-C", str(target), "config", "--get", key],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@unittest.skipIf(GIT is None, "real git binary not on PATH")
class MountAutoBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._home_tmp = tempfile.TemporaryDirectory()
        self._home_dir = Path(self._home_tmp.name) / "home"
        self._home_dir.mkdir(parents=True)
        self._bin_dir = Path(self._home_tmp.name) / "bin"
        self._bin_dir.mkdir()
        helper = self._bin_dir / "git-remote-trunks"
        repo_root = Path(__file__).resolve().parents[2]
        helper.write_text(
            "#!/bin/sh\n"
            f"export PYTHONPATH={shlex.quote(str(repo_root))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"exec {shlex.quote(sys.executable)} -m trunks.remote_helper \"$@\"\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        self._home_patch = patch.dict(
            os.environ,
            {
                "TRUNKS_HOME": str(self._home_dir),
                "TRUNKS_REAL_GIT": GIT or "",
                "PATH": f"{self._bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            },
            clear=False,
        )
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        self._home_tmp.cleanup()

    async def _mount(self, *args: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = await dispatch(["mount", *args])
        self.assertEqual(rc, 0, msg=out.getvalue())
        return out.getvalue()

    async def test_single_primary_storage_auto_wires_origin(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.minio]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/storage.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            output = await self._mount("--repo", "myrepo", "--path", str(target))
            self.assertIn("Git         created", output)
            self.assertIn("Origin      added -> trunks://minio/myrepo", output)
            git_remote = subprocess.run(
                [GIT, "-C", str(target), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(git_remote.stdout.strip(), "trunks://minio/myrepo")
            self.assertEqual(_git_config(target, "branch.main.remote"), "origin")
            self.assertEqual(_git_config(target, "branch.main.merge"), "refs/heads/main")
            self.assertTrue((target / ".git").exists())

    async def test_remount_is_idempotent(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.minio]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/storage.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            await self._mount("--repo", "myrepo", "--path", str(target))
            second = await self._mount("--repo", "myrepo", "--path", str(target))
            self.assertIn("Git         exists", second)
            self.assertIn("Origin      already trunks (trunks://minio/myrepo)", second)

    async def test_preserves_conflicting_origin(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.minio]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/storage.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            target.mkdir()
            subprocess.run([GIT, "-C", str(target), "init", "--initial-branch=main", "-q"], check=True)
            subprocess.run(
                [GIT, "-C", str(target), "remote", "add", "origin", "git@example.com:keep/this.git"],
                check=True,
            )
            output = await self._mount("--repo", "myrepo", "--path", str(target))
            self.assertIn("Git         exists", output)
            self.assertIn("Origin      kept (git@example.com:keep/this.git)", output)
            git_remote = subprocess.run(
                [GIT, "-C", str(target), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(git_remote.stdout.strip(), "git@example.com:keep/this.git")

    async def test_multiple_primaries_require_explicit_storage(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.minio]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/a.db#{repo}\n"
            "[storage.s3]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/b.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            output = await self._mount("--repo", "myrepo", "--path", str(target))
            self.assertIn("Origin      not wired", output)
            self.assertIn("multiple primary storages", output)
            self.assertFalse((target / ".git").exists())

    async def test_explicit_storage_flag_picks_named_profile(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.minio]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/a.db#{repo}\n"
            "[storage.s3]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/b.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            output = await self._mount("--repo", "myrepo", "--path", str(target), "--storage", "s3")
            self.assertIn("Origin      added -> trunks://s3/myrepo", output)
            git_remote = subprocess.run(
                [GIT, "-C", str(target), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(git_remote.stdout.strip(), "trunks://s3/myrepo")

    async def test_plain_git_push_after_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage_db = Path(tmp) / "storage.db"
            _write_global_config(
                self._home_dir,
                f"[storage.primary]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{storage_db}#{{repo}}\n",
            )
            target = Path(tmp) / "myrepo"
            await self._mount("--repo", "myrepo", "--path", str(target))
            subprocess.run([GIT, "-C", str(target), "config", "user.email", "test@example.com"], check=True)
            subprocess.run([GIT, "-C", str(target), "config", "user.name", "Test"], check=True)
            (target / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run([GIT, "-C", str(target), "add", "README.md"], check=True)
            subprocess.run([GIT, "-C", str(target), "commit", "-m", "init", "-q"], check=True)

            push = subprocess.run(
                [GIT, "-C", str(target), "push"],
                capture_output=True,
                text=True,
            )

            self.assertEqual(push.returncode, 0, msg=f"stdout:\n{push.stdout}\nstderr:\n{push.stderr}")

    async def test_storage_add_then_mount_in_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                store = Path(tmp) / "store"
                add_out = io.StringIO()
                with redirect_stdout(add_out):
                    rc = await dispatch([
                        "storage",
                        "add",
                        "--name",
                        "primary",
                        "--backend",
                        "local",
                        "--path",
                        str(store),
                    ])
                self.assertEqual(rc, 0, msg=add_out.getvalue())
                self.assertIn("local://", add_out.getvalue())

                target = Path(tmp) / "myrepo"
                target.mkdir()
                os.chdir(target)
                mount_out = io.StringIO()
                with redirect_stdout(mount_out):
                    rc = await dispatch(["mount", "--repo", "myrepo"])
                self.assertEqual(rc, 0, msg=mount_out.getvalue())
                self.assertIn("Origin      added -> trunks://primary/myrepo", mount_out.getvalue())

                subprocess.run([GIT, "-C", str(target), "config", "user.email", "test@example.com"], check=True)
                subprocess.run([GIT, "-C", str(target), "config", "user.name", "Test"], check=True)
                (target / "README.md").write_text("hello\n", encoding="utf-8")
                subprocess.run([GIT, "-C", str(target), "add", "README.md"], check=True)
                subprocess.run([GIT, "-C", str(target), "commit", "-m", "init", "-q"], check=True)
                push = subprocess.run([GIT, "-C", str(target), "push"], capture_output=True, text=True)
                self.assertEqual(push.returncode, 0, msg=f"stdout:\n{push.stdout}\nstderr:\n{push.stderr}")
            finally:
                os.chdir(cwd)

    async def test_storage_flag_unknown_profile_skips_with_hint(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.minio]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/a.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            output = await self._mount("--repo", "myrepo", "--path", str(target), "--storage", "missing")
            self.assertIn("Origin      not wired", output)
            self.assertIn("'missing' not found", output)
            self.assertFalse((target / ".git").exists())

    async def test_storage_flag_rejects_mirror_profile(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.primary]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/a.db#{repo}\n"
            "[storage.backup]\nbackend = url\nrole = mirror\nsetting.url = sqlite:///{tmp}/b.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            output = await self._mount("--repo", "myrepo", "--path", str(target), "--storage", "backup")
            self.assertIn("Origin      not wired", output)
            self.assertIn("'backup' is mirror, not primary", output)
            self.assertFalse((target / ".git").exists())

    async def test_no_git_flag_skips_bootstrap(self) -> None:
        _write_global_config(
            self._home_dir,
            "[storage.minio]\nbackend = url\nrole = primary\nsetting.url = sqlite:///{tmp}/a.db#{repo}\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            output = await self._mount("--repo", "myrepo", "--path", str(target), "--no-git")
            self.assertNotIn("Git         ", output)
            self.assertNotIn("Origin      ", output[output.index("Status"):])
            self.assertFalse((target / ".git").exists())

    async def test_no_storage_configured_skips_silently_with_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "myrepo"
            output = await self._mount("--repo", "myrepo", "--path", str(target))
            self.assertIn("Origin      not wired", output)
            self.assertIn("no storage configured", output)
            self.assertFalse((target / ".git").exists())


if __name__ == "__main__":
    unittest.main()
