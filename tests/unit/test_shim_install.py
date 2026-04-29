from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trunks.cli import _shim_script, shim
from trunks.gitcache import _shim_marker_path


PACKAGE_PARENT = Path(__file__).resolve().parents[2]


class ShimInstallTests(unittest.TestCase):
    def test_shim_script_bakes_package_parent_into_pythonpath(self) -> None:
        script = _shim_script()
        self.assertIn(f'export PYTHONPATH="{PACKAGE_PARENT}', script)
        # Ensure the user's runtime PYTHONPATH still layers in.
        self.assertIn('${PYTHONPATH:+:$PYTHONPATH}', script)
        # Trunks gitshim is the entry point.
        self.assertIn(f'exec "{sys.executable}" -m trunks.gitshim', script)

    def test_shim_install_writes_executable_with_python_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            dest = Path(tmp)
            with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                rc = shim("install", str(dest))
            self.assertEqual(rc, 0)
            installed = dest / "git"
            self.assertTrue(installed.exists())
            self.assertTrue(os.access(installed, os.X_OK))
            content = installed.read_text(encoding="utf-8")
            self.assertIn(f'export PYTHONPATH="{PACKAGE_PARENT}', content)

    def test_shim_install_writes_marker_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            dest = Path(tmp)
            xdg = Path(fake_home) / ".config"
            with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(xdg)}, clear=False):
                self.assertEqual(shim("install", str(dest)), 0)
                marker = _shim_marker_path()
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), "1")
            # Marker landed under XDG_CONFIG_HOME, not /Users/.config
            self.assertTrue(str(marker).startswith(str(xdg)))

    def test_installed_shim_runs_with_clean_subprocess_env(self) -> None:
        # Smoke test: the bash script the installer wrote must be runnable
        # from a subprocess that has no PYTHONPATH in its inherited env. The
        # shim's own export bakes the package parent in, so the python -m call
        # resolves trunks.gitshim. Outside any trunks repo, the shim falls
        # through to system git (--version) which always exits 0.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_home:
            dest = Path(tmp)
            with patch.dict(os.environ, {"HOME": fake_home, "XDG_CONFIG_HOME": str(Path(fake_home) / ".config")}, clear=False):
                self.assertEqual(shim("install", str(dest)), 0)
            installed = dest / "git"
            clean_env = {
                "HOME": fake_home,
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            }
            result = subprocess.run(
                ["/bin/bash", str(installed), "--version"],
                capture_output=True,
                text=True,
                env=clean_env,
                timeout=30,
            )
            # Exit 0 = python interpreter found trunks module + delegated to system git
            # successfully. Anything else means PYTHONPATH bake failed.
            self.assertEqual(result.returncode, 0, msg=f"stderr: {result.stderr}")
            self.assertIn("git version", result.stdout)

    def test_runtime_pythonpath_layers_on_top_of_baked_value(self) -> None:
        # The shim emits ${PYTHONPATH:+:$PYTHONPATH} so the user's ambient
        # PYTHONPATH wins-or-augments. Verify by reading the script source.
        script = _shim_script()
        # Assert the trailing ${PYTHONPATH:+:$PYTHONPATH} is present so layering works
        self.assertRegex(script, r'export PYTHONPATH="[^"]+\$\{PYTHONPATH:\+:\$PYTHONPATH\}"')


if __name__ == "__main__":
    unittest.main()
