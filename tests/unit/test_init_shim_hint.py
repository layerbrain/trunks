from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch


class InitShimHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_without_git_dir_prints_no_git_interop_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            env = {"HOME": fake_home, "PATH": "/usr/bin:/bin"}
            try:
                with patch.dict(os.environ, env, clear=False):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["init"]), 0)
                    self.assertNotIn("trunks shim install", out.getvalue())
                    self.assertNotIn("git interop", out.getvalue())
                    self.assertNotIn("managed shell", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_init_does_not_require_global_shim_when_installed_and_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            shim_dir = Path(fake_home) / ".local" / "bin"
            shim_dir.mkdir(parents=True, exist_ok=True)
            shim = shim_dir / "git"
            shim.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            shim.chmod(0o755)
            env = {"HOME": fake_home, "PATH": f"{shim_dir}:/usr/bin:/bin"}
            try:
                with patch.dict(os.environ, env, clear=False):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["init"]), 0)
                    self.assertNotIn("trunks shim install", out.getvalue())
                    self.assertNotIn("global", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_init_with_existing_git_dir_does_not_print_git_interop_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            cwd = Path.cwd()
            os.chdir(root)
            env = {"HOME": fake_home, "PATH": "/usr/bin:/bin"}
            try:
                with patch.dict(os.environ, env, clear=False):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(await dispatch(["init"]), 0)
                    self.assertNotIn("managed shell", out.getvalue())
                    self.assertNotIn("trunks shim install", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_mount_without_git_dir_prints_no_git_interop_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            root = Path(tmp)
            cwd = Path.cwd()
            os.chdir(root)
            env = {"HOME": fake_home, "PATH": "/usr/bin:/bin"}
            try:
                with patch.dict(os.environ, env, clear=False):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(
                            await dispatch(["mount", "--repo", "demo", "--path", str(root / "demo"), "--mode", "disk"]),
                            0,
                        )
                    self.assertNotIn("trunks shim install", out.getvalue())
                    self.assertNotIn("git interop", out.getvalue())
                    self.assertNotIn("managed shell", out.getvalue())
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
