from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.cli import dispatch
from trunks.mount import nfs
from trunks.mount.lifecycle import MountInfo, _mount_command
from trunks.mount.rpc import (
    AUTH_NONE,
    LAST_FRAG_BIT,
    MSG_CALL,
    MSG_REPLY,
    MSG_ACCEPTED,
    SUCCESS,
    encode_record,
)
from trunks.mount.xdr import Packer, Unpacker


def _build_null_call() -> bytes:
    p = Packer()
    p.pack_uint32(99)  # xid
    p.pack_uint32(MSG_CALL)
    p.pack_uint32(2)
    p.pack_uint32(nfs.PROGRAM)
    p.pack_uint32(3)
    p.pack_uint32(nfs.NFSPROC3_NULL)
    p.pack_uint32(AUTH_NONE)
    p.pack_opaque(b"")
    p.pack_uint32(AUTH_NONE)
    p.pack_opaque(b"")
    return p.bytes()


async def _round_trip_null(port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(encode_record(_build_null_call()))
        await writer.drain()
        header = await reader.readexactly(4)
        size = struct.unpack(">I", header)[0] & ~LAST_FRAG_BIT
        body = await reader.readexactly(size)
    finally:
        writer.close()
        await writer.wait_closed()
    unp = Unpacker(body)
    assert unp.unpack_uint32() == 99
    assert unp.unpack_uint32() == MSG_REPLY
    assert unp.unpack_uint32() == MSG_ACCEPTED
    unp.unpack_uint32()
    unp.unpack_opaque()
    assert unp.unpack_uint32() == SUCCESS


class MountDaemonSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_mount_command_fails_fast_if_daemon_stops(self) -> None:
        command = _mount_command(MountInfo(target=Path("/tmp/trunks-mnt"), host="127.0.0.1", port=12345, export="/demo"))
        joined = " ".join(command)
        self.assertIn("soft", joined)
        self.assertIn("intr", joined)
        self.assertIn("retrans=2", joined)
        self.assertIn("timeo=20", joined)
        self.assertIn("deadtimeout=5", joined)
        self.assertIn("retrycnt=0", joined)
        self.assertIn("rsize=1048576", joined)
        self.assertIn("wsize=1048576", joined)

    async def test_daemon_subprocess_serves_then_shuts_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            target = root / "target"
            repo_path.mkdir()
            target.mkdir()

            cwd = Path.cwd()
            os.chdir(repo_path)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
            finally:
                os.chdir(cwd)

            runtime_file = repo_path / ".trunks" / "runtime" / "virtual.json"

            env = os.environ.copy()
            env["TRUNKS_NFS_SKIP_OS_MOUNT"] = "1"
            env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[2]))

            proc = subprocess.Popen(  # noqa: S603 — controlled args
                [
                    sys.executable,
                    "-m",
                    "trunks.mount.daemon",
                    "--repo-path",
                    str(repo_path),
                    "--target",
                    str(target),
                    "--export",
                    "demo",
                    "--runtime-file",
                    str(runtime_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )

            try:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    if proc.poll() is not None:
                        stdout, stderr = proc.communicate()
                        self.fail(
                            f"daemon exited early ({proc.returncode}):\n"
                            f"stdout={stdout.decode()}\nstderr={stderr.decode()}"
                        )
                    if runtime_file.exists():
                        try:
                            payload = json.loads(runtime_file.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            await asyncio.sleep(0.1)
                            continue
                        break
                    await asyncio.sleep(0.1)
                else:
                    self.fail("daemon never wrote runtime file")

                self.assertEqual(payload["export"], "/demo")
                self.assertEqual(payload["host"], "127.0.0.1")
                self.assertGreater(int(payload["port"]), 0)
                self.assertEqual(int(payload["pid"]), proc.pid)

                await _round_trip_null(int(payload["port"]))
            finally:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.communicate(timeout=10.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=5.0)

            self.assertFalse(runtime_file.exists(), "runtime file was not removed on shutdown")

    async def test_daemon_exits_when_mount_target_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            target = root / "target"
            repo_path.mkdir()
            target.mkdir()

            cwd = Path.cwd()
            os.chdir(repo_path)
            try:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await dispatch(["init", "--name", "demo"]), 0)
            finally:
                os.chdir(cwd)

            runtime_file = repo_path / ".trunks" / "runtime" / "virtual.json"

            env = os.environ.copy()
            env["TRUNKS_NFS_SKIP_OS_MOUNT"] = "1"
            env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[2]))

            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "trunks.mount.daemon",
                    "--repo-path",
                    str(repo_path),
                    "--target",
                    str(target),
                    "--export",
                    "demo",
                    "--runtime-file",
                    str(runtime_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )

            try:
                deadline = time.time() + 10.0
                while time.time() < deadline and not runtime_file.exists():
                    if proc.poll() is not None:
                        stdout, stderr = proc.communicate()
                        self.fail(
                            f"daemon exited early ({proc.returncode}):\n"
                            f"stdout={stdout.decode()}\nstderr={stderr.decode()}"
                        )
                    await asyncio.sleep(0.1)
                self.assertTrue(runtime_file.exists(), "daemon never wrote runtime file")

                shutil.rmtree(target)
                deadline = time.time() + 5.0
                while time.time() < deadline and proc.poll() is None:
                    await asyncio.sleep(0.1)
                self.assertIsNotNone(proc.poll(), "daemon did not exit after target was removed")
                self.assertFalse(runtime_file.exists(), "runtime file was not removed after target disappeared")
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.communicate(timeout=10.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=5.0)
                else:
                    proc.communicate(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
