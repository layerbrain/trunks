"""Unit tests for the post-mount writability probe.

The probe is what catches sandbox profiles that allow `mount(2)` but block
subsequent NFS reads/writes. Because the failure is a kernel-level decision,
we cannot reproduce it from inside Python — but we can mock the underlying
filesystem call to raise `PermissionError`, which is exactly what the kernel
hands us in the real failure mode (verified empirically: Claude Code's
OPERON_SANDBOXED_NETWORK=1 sandbox returns EPERM on `open()` against an NFS
mount even when ACCESS3 has granted full bits).

These tests stay completely in-process. Real-OS-mount coverage lives in
`tests/unit/test_mount_nfs_real.py` and is gated on TRUNKS_RUN_REAL_NFS_TESTS.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trunks.mount.lifecycle import (
    MountError,
    MountSandboxedError,
    verify_mount_writable,
)


class VerifyMountWritableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name)
        # The probe is a no-op when TRUNKS_NFS_SKIP_OS_MOUNT=1 is in effect
        # (tests for in-process daemon supervisor wiring rely on that escape
        # hatch). Make sure these tests run with it cleared.
        self.saved = os.environ.pop("TRUNKS_NFS_SKIP_OS_MOUNT", None)

    async def asyncTearDown(self) -> None:
        if self.saved is not None:
            os.environ["TRUNKS_NFS_SKIP_OS_MOUNT"] = self.saved
        self.tmp.cleanup()

    async def test_probe_succeeds_on_writable_directory(self) -> None:
        await verify_mount_writable(self.target)
        # Probe leaves no residue.
        self.assertFalse((self.target / ".trunks-mount-probe").exists())

    async def test_probe_raises_sandboxed_error_on_eperm(self) -> None:
        with mock.patch.object(
            Path, "write_bytes", side_effect=PermissionError(1, "Operation not permitted")
        ):
            with self.assertRaises(MountSandboxedError) as ctx:
                await verify_mount_writable(self.target)
        message = str(ctx.exception)
        self.assertIn("kernel rejected NFS probe write with EPERM", message)
        self.assertIn("OPERON_SANDBOXED_NETWORK", message)
        self.assertIn(str(self.target), message)
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)

    async def test_probe_eperm_on_read_also_classified_as_sandboxed(self) -> None:
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError(1, "Operation not permitted")
        ):
            with self.assertRaises(MountSandboxedError):
                await verify_mount_writable(self.target)
        # Even though write succeeded, we leave the probe file behind on
        # failure: the caller is responsible for tearing down the mount, which
        # will discard the entire directory state anyway.

    async def test_probe_eperm_on_unlink_also_classified_as_sandboxed(self) -> None:
        with mock.patch.object(
            Path, "unlink", side_effect=PermissionError(1, "Operation not permitted")
        ):
            with self.assertRaises(MountSandboxedError):
                await verify_mount_writable(self.target)

    async def test_probe_round_trip_mismatch_raises_plain_mount_error(self) -> None:
        # If write succeeds and read returns wrong bytes, that is NOT a
        # sandbox issue — that is the kernel client serving stale cached
        # data. Surface it as a plain MountError so the caller does not
        # mis-attribute it to a sandbox.
        original_read = Path.read_bytes

        def stale_read(self_path: Path) -> bytes:  # type: ignore[no-untyped-def]
            if self_path.name == ".trunks-mount-probe":
                return b"unexpected stale data"
            return original_read(self_path)

        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=stale_read):
            with self.assertRaises(MountError) as ctx:
                await verify_mount_writable(self.target)
        self.assertNotIsInstance(ctx.exception, MountSandboxedError)
        self.assertIn("round-trip mismatch", str(ctx.exception))

    async def test_probe_skipped_when_skip_env_set(self) -> None:
        os.environ["TRUNKS_NFS_SKIP_OS_MOUNT"] = "1"
        try:
            # Even if the underlying filesystem would refuse, the probe is a
            # no-op when the test escape hatch is in effect.
            with mock.patch.object(
                Path, "write_bytes", side_effect=PermissionError(1, "nope")
            ):
                await verify_mount_writable(self.target)
        finally:
            os.environ.pop("TRUNKS_NFS_SKIP_OS_MOUNT", None)

    async def test_sandboxed_error_is_subclass_of_mount_error(self) -> None:
        # Callers that catch `MountError` for cleanup logic should still
        # see sandboxed failures, which are a strict refinement.
        self.assertTrue(issubclass(MountSandboxedError, MountError))


if __name__ == "__main__":
    unittest.main()
