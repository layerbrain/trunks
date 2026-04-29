from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.cli import dispatch
from trunks.mount import registry as mount_registry
from trunks.repository import Repository


class CliVirtualMountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "mnt"
        # `target` must not exist yet — _mount_virtual creates the empty dir.
        self.env_keys = ("TRUNKS_HOME", "TRUNKS_LOCAL_ONLY", "TRUNKS_NFS_SKIP_OS_MOUNT")
        self.saved_env = {key: os.environ.get(key) for key in self.env_keys}
        os.environ["TRUNKS_HOME"] = str(self.root / "home")
        os.environ["TRUNKS_LOCAL_ONLY"] = "1"
        os.environ["TRUNKS_NFS_SKIP_OS_MOUNT"] = "1"

    async def asyncTearDown(self) -> None:
        # Defensive: try to unmount in case the test failed mid-flight.
        record = mount_registry.read(self.target)
        if record is not None:
            try:
                with redirect_stdout(io.StringIO()):
                    await dispatch(["unmount", "--path", str(self.target)])
            except Exception:
                pass
        for key, val in self.saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    async def test_mount_writes_registry_and_unmount_clears_it(self) -> None:
        with redirect_stdout(io.StringIO()) as buf:
            rc = await dispatch(["mount", "--repo", "demo", "--path", str(self.target), "--mode", "virtual"])
        self.assertEqual(rc, 0, buf.getvalue())

        record = mount_registry.read(self.target)
        self.assertIsNotNone(record, "registry record was not written")
        self.assertRegex(record.export, r"^/demo-[0-9a-z]{26}$")
        self.assertEqual(record.host, "127.0.0.1")
        self.assertGreater(record.port, 0)
        self.assertGreater(record.pid, 0)

        # Repo data path is materialized.
        self.assertTrue(record.repo_path.exists())
        repo = Repository.find(record.repo_path)
        self.assertEqual(repo.name, "demo")

        with redirect_stdout(io.StringIO()) as buf:
            rc = await dispatch(["unmount", "--path", str(self.target)])
        self.assertEqual(rc, 0, buf.getvalue())

        self.assertIsNone(mount_registry.read(self.target))

        # Daemon should have exited; give it a moment then verify.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                os.kill(record.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(record.pid, 0)
                self.fail(f"daemon pid {record.pid} still alive after unmount")
            except ProcessLookupError:
                pass

    async def test_mount_requires_path(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = await dispatch(["mount", "--repo", "demo", "--mode", "virtual"])
        self.assertEqual(rc, 1)

    async def test_mount_reaps_dead_virtual_record(self) -> None:
        self.target.mkdir(parents=True)
        repo_path = self.root / "home" / "repos" / "demo"
        mount_registry.write(
            mount_registry.Record(
                target=self.target,
                repo_path=repo_path,
                export="/demo",
                host="127.0.0.1",
                port=1,
                pid=999_999_999,
            )
        )

        with redirect_stdout(io.StringIO()) as buf:
            rc = await dispatch(["mount", "--repo", "demo", "--path", str(self.target), "--mode", "virtual"])
        self.assertEqual(rc, 0, buf.getvalue())
        record = mount_registry.read(self.target)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertNotEqual(record.pid, 999_999_999)

    async def test_commands_inside_virtual_mount_use_backing_repo(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(await dispatch(["mount", "--repo", "demo", "--path", str(self.target), "--mode", "virtual"]), 0)
        record = mount_registry.read(self.target)
        self.assertIsNotNone(record)
        assert record is not None
        repo = Repository.find(record.repo_path)
        repo.write_file("agent.txt", b"done")

        cwd = Path.cwd()
        os.chdir(self.target)
        try:
            with redirect_stdout(io.StringIO()) as buf:
                rc = await dispatch(["checkpoint", "-m", "agent output"])
            self.assertEqual(rc, 0, buf.getvalue())

            with redirect_stdout(io.StringIO()) as buf:
                rc = await dispatch(["status", "--json"])
            self.assertEqual(rc, 0, buf.getvalue())
        finally:
            os.chdir(cwd)

        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["virtual"])
        self.assertEqual(payload["path"], str(self.target.resolve()))
        self.assertEqual(Path(payload["repo_data_path"]).resolve(), record.repo_path.resolve())
        self.assertFalse(payload["dirty"])
        self.assertEqual(repo.load_commit(repo.ref("main")).message, "agent output")


if __name__ == "__main__":
    unittest.main()
