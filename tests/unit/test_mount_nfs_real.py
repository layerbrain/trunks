"""Real OS-level NFS mount test for `trunks mount`.

Skipped unless `TRUNKS_RUN_REAL_NFS_TESTS=1` so CI hosts that lack a working
NFS client (or that disallow loopback NFS mounts) don't fail. To run locally:

    TRUNKS_RUN_REAL_NFS_TESTS=1 python -m unittest tests.unit.test_mount_nfs_real

Unlike the unit suite, this test deliberately does NOT set
`TRUNKS_NFS_SKIP_OS_MOUNT=1`. It exercises the full path: NFS server →
`mount_nfs`/`mount -t nfs` → kernel client → real filesystem ops through the
mount point.
"""

from __future__ import annotations

import io
import json
import os
import platform
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from statistics import median
from pathlib import Path

from trunks.mount import registry as mount_registry
from trunks.mount import mountd
from trunks.mount.rpc import AUTH_NONE, LAST_FRAG_BIT, MSG_ACCEPTED, MSG_CALL, MSG_REPLY, SUCCESS
from trunks.mount.xdr import Packer, Unpacker
from trunks.repository import Repository


_RUN_REAL = os.environ.get("TRUNKS_RUN_REAL_NFS_TESTS") == "1"
_REAL_EXPORT_PREFIX = "127.0.0.1:/real-"
_MIN_REAL_NFS_MEDIAN_WRITE_MB_S = 200.0
_MIN_REAL_NFS_SAMPLE_WRITE_MB_S = 100.0
_MAX_REAL_NFS_SAMPLE_SPREAD = 4.0


def _mount_table_contains(target: Path) -> bool:
    proc = subprocess.run(["mount"], capture_output=True, text=True, check=False)
    resolved = str(target.resolve())
    return resolved in proc.stdout


def _force_umount(target: Path) -> None:
    if _mount_table_contains(target):
        subprocess.run(["umount", "-f", str(target)], check=False, timeout=10)


def _real_test_mount_targets() -> list[Path]:
    proc = subprocess.run(["mount"], capture_output=True, text=True, check=False)
    targets: list[Path] = []
    for line in proc.stdout.splitlines():
        if not line.startswith(_REAL_EXPORT_PREFIX) or " on " not in line:
            continue
        _, rest = line.split(" on ", 1)
        target, _, _ = rest.partition(" (")
        if target:
            targets.append(Path(target))
    return targets


def _cleanup_real_test_leftovers() -> None:
    for target in _real_test_mount_targets():
        subprocess.run(["umount", "-f", str(target)], check=False, timeout=10)

    proc = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False)
    for line in proc.stdout.splitlines():
        text = line.strip()
        if "trunks.mount.daemon" not in text or "--export real-" not in text:
            continue
        pid_text, _, _ = text.partition(" ")
        try:
            os.kill(int(pid_text), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def _mountd_mnt_status(port: int, export: str) -> int:
    args = Packer()
    args.pack_string(export)

    call = Packer()
    call.pack_uint32(7)
    call.pack_uint32(MSG_CALL)
    call.pack_uint32(2)
    call.pack_uint32(mountd.PROGRAM)
    call.pack_uint32(mountd.VERSION)
    call.pack_uint32(mountd.MOUNTPROC3_MNT)
    call.pack_uint32(AUTH_NONE)
    call.pack_opaque(b"")
    call.pack_uint32(AUTH_NONE)
    call.pack_opaque(b"")
    payload = call.bytes() + args.bytes()
    frame = struct.pack(">I", LAST_FRAG_BIT | len(payload)) + payload

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(frame)
        header = sock.recv(4)
        size = struct.unpack(">I", header)[0] & ~LAST_FRAG_BIT
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                break
            data += chunk

    unp = Unpacker(data)
    assert unp.unpack_uint32() == 7
    assert unp.unpack_uint32() == MSG_REPLY
    assert unp.unpack_uint32() == MSG_ACCEPTED
    unp.unpack_uint32()
    unp.unpack_opaque()
    assert unp.unpack_uint32() == SUCCESS
    return unp.unpack_uint32()


@unittest.skipUnless(_RUN_REAL, "set TRUNKS_RUN_REAL_NFS_TESTS=1 to run real NFS mount tests")
class RealNfsMountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _cleanup_real_test_leftovers()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "mnt"
        self.repo_root = Path(__file__).resolve().parents[2]
        self.env_keys = ("TRUNKS_HOME", "TRUNKS_LOCAL_ONLY", "TRUNKS_NFS_SKIP_OS_MOUNT")
        self.saved_env = {key: os.environ.get(key) for key in self.env_keys}
        os.environ["TRUNKS_HOME"] = str(self.root / "home")
        os.environ["TRUNKS_LOCAL_ONLY"] = "1"
        os.environ.pop("TRUNKS_NFS_SKIP_OS_MOUNT", None)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = (
            f"{self.repo_root}{os.pathsep}{self.env['PYTHONPATH']}"
            if self.env.get("PYTHONPATH")
            else str(self.repo_root)
        )

    async def asyncTearDown(self) -> None:
        record = mount_registry.read(self.target)
        if record is not None:
            self._run_cli("unmount", "--path", str(self.target), check=False)
        _force_umount(self.target)
        for key, val in self.saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()
        _cleanup_real_test_leftovers()

    async def test_real_mount_round_trip(self) -> None:
        mount = self._run_cli(
            "mount", "--repo", "real-smoke", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount.returncode, 0, mount.stdout + mount.stderr)

        self.assertTrue(
            _mount_table_contains(self.target),
            f"target not in mount table: {self.target}",
        )

        # Phase C contract: kext-free. macFUSE / osxfuse / FUSE-T all leave
        # entries in kextstat. If anything in our stack accidentally
        # bootstraps a FUSE kext, this catches it before users hit a
        # System-Extension-Blocked dialog. See plan §1.3 / §6.
        if platform.system() == "Darwin":
            kextstat = subprocess.run(
                ["kextstat"], capture_output=True, text=True, check=False, timeout=10
            )
            self.assertEqual(kextstat.returncode, 0, kextstat.stderr)
            fuse_lines = [
                line for line in kextstat.stdout.splitlines()
                if "fuse" in line.lower()
            ]
            self.assertEqual(
                fuse_lines, [], f"FUSE kext detected:\n" + "\n".join(fuse_lines)
            )

        # Write through a real shell process → kernel NFS client → daemon NFS
        # handler → repo data dir. This catches macOS EPERM failures that
        # in-process handler tests cannot see.
        shell = subprocess.run(
            ["/bin/sh", "-lc", "echo 'hi from real nfs' > hello.txt && cat hello.txt"],
            cwd=self.target,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)
        self.assertEqual(shell.stdout, "hi from real nfs\n")

        # Directory listing through the kernel NFS client should show only
        # real files (AppleDouble sidecars stay ephemeral in the daemon).
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["hello.txt"])

        record = mount_registry.read(self.target)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(_mountd_mnt_status(record.port, record.export), mountd.MNT3ERR_ACCES)

        # Checkpoint from inside the mount path through the real CLI: the
        # registry resolves cwd to the backing repo data dir.
        checkpoint = self._run_cli("checkpoint", "-m", "real nfs write", cwd=self.target)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stdout + checkpoint.stderr)
        status = self._run_cli("status", "--json", cwd=self.target)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)

        payload = json.loads(status.stdout)
        self.assertTrue(payload["virtual"])
        self.assertEqual(payload["path"], str(self.target.resolve()))
        self.assertFalse(payload["dirty"])

        repo = Repository.find(record.repo_path)
        head = repo.ref(repo.current_branch)
        self.assertIsNotNone(head)
        assert head is not None
        self.assertEqual(repo.load_commit(head).message, "real nfs write")
        self.assertEqual(repo.read_file("hello.txt"), b"hi from real nfs\n")

        unmount = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount.returncode, 0, unmount.stdout + unmount.stderr)

        self.assertFalse(
            _mount_table_contains(self.target),
            f"target still mounted after unmount: {self.target}",
        )

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

    async def test_remount_reaps_dead_daemon_and_stale_kernel_mount(self) -> None:
        mount = self._run_cli(
            "mount", "--repo", "real-smoke", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount.returncode, 0, mount.stdout + mount.stderr)
        first = mount_registry.read(self.target)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertTrue(_mount_table_contains(self.target))

        os.kill(first.pid, 9)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                os.kill(first.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)

        remount = self._run_cli(
            "mount", "--repo", "real-smoke", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(remount.returncode, 0, remount.stdout + remount.stderr)
        second = mount_registry.read(self.target)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotEqual(second.pid, first.pid)
        self.assertTrue(_mount_table_contains(self.target))

        shell = subprocess.run(
            ["/bin/sh", "-lc", "echo recovered > recovered.txt && cat recovered.txt"],
            cwd=self.target,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)
        self.assertEqual(shell.stdout, "recovered\n")

        unmount = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount.returncode, 0, unmount.stdout + unmount.stderr)
        self.assertFalse(_mount_table_contains(self.target))

    async def test_real_mount_edge_matrix(self) -> None:
        mount = self._run_cli(
            "mount", "--repo", "real-edges", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount.returncode, 0, mount.stdout + mount.stderr)
        self.assertTrue(_mount_table_contains(self.target))

        script = r"""
set -eu
printf 'space\n' > "space name.txt"
mv "space name.txt" "renamed space.txt"
rm "renamed space.txt"

mkdir -p "dir/sub/deep"
printf 'alpha' > "dir/sub/deep/file.txt"
printf 'beta' >> "dir/sub/deep/file.txt"
mv "dir/sub/deep/file.txt" "dir/sub/deep/file-renamed.txt"
rm "dir/sub/deep/file-renamed.txt"
printf 'gamma' > "dir/sub/deep/file-renamed.txt"

printf 'payload' > truncate.txt
: > truncate.txt
test ! -s truncate.txt

printf 'hidden' > .dotfile
printf 'unicode' > "unicodé-文件.txt"
mkdir empty-dir
mkdir dir/to-remove
rmdir dir/to-remove

dd if=/dev/zero of=blob.bin bs=1048576 count=2 >/dev/null 2>&1
test "$(wc -c < blob.bin | tr -d ' ')" = "2097152"
rm blob.bin

if ln -s target symlink 2>/dev/null; then
    exit 44
fi
test ! -e symlink
"""
        shell = subprocess.run(
            ["/bin/sh", "-lc", script],
            cwd=self.target,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)

        root_names = sorted(path.name for path in self.target.iterdir())
        self.assertIn(".dotfile", root_names)
        self.assertIn("dir", root_names)
        self.assertIn("empty-dir", root_names)
        self.assertIn("truncate.txt", root_names)
        self.assertIn("unicodé-文件.txt", root_names)
        self.assertNotIn("space name.txt", root_names)
        self.assertNotIn("renamed space.txt", root_names)
        self.assertNotIn("blob.bin", root_names)
        self.assertNotIn("symlink", root_names)

        checkpoint = self._run_cli("checkpoint", "-m", "real nfs edge matrix", cwd=self.target)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stdout + checkpoint.stderr)

        record = mount_registry.read(self.target)
        self.assertIsNotNone(record)
        assert record is not None
        repo = Repository.find(record.repo_path)
        self.assertEqual(repo.read_file("dir/sub/deep/file-renamed.txt"), b"gamma")
        self.assertEqual(repo.read_file(".dotfile"), b"hidden")
        self.assertEqual(repo.read_file("unicodé-文件.txt"), b"unicode")
        self.assertEqual(repo.read_file("truncate.txt"), b"")
        self.assertTrue(repo.exists("empty-dir"))
        self.assertFalse(repo.exists("space name.txt"))
        self.assertFalse(repo.exists("renamed space.txt"))
        self.assertFalse(repo.exists("blob.bin"))
        self.assertFalse(repo.exists("symlink"))

        unmount = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount.returncode, 0, unmount.stdout + unmount.stderr)
        self.assertFalse(_mount_table_contains(self.target))

    async def test_real_mount_protocol_edges(self) -> None:
        """Cases that exercise wire-protocol paths the basic round-trip misses:
        concurrent writers, write past the write-size boundary (kernel splits into
        multiple WRITE3 calls — the daemon must coalesce them via the
        in-daemon write buffer), mid-file offset writes, random-access reads,
        readdir cookie pagination, and hard-link rejection (proc_link →
        NFS3ERR_NOTSUPP)."""
        mount = self._run_cli(
            "mount", "--repo", "real-protocol", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount.returncode, 0, mount.stdout + mount.stderr)
        self.assertTrue(_mount_table_contains(self.target))

        # 1. Two concurrent writers to different files → both must succeed.
        # 2. 2 MB single file in one open() → kernel issues multiple WRITE3 calls
        #    and the daemon must coalesce them into one repo blob.
        # 3. Mid-file offset write: pwrite at byte 1000 of a 5000-byte file.
        # 4. Random-access read: open + seek + read at byte 100000 of a 200 KB
        #    file → kernel issues a single READ3 at offset 100000.
        # 5. readdir pagination: 200 entries in one dir → kernel paginates
        #    via cookies; ls must return all of them in stable order.
        # 6. Hard link → ln must fail (proc_link returns NFS3ERR_NOTSUPP).
        script = textwrap.dedent(r"""
            set -eu
            # 1. concurrent writers
            ( for i in $(seq 1 50); do printf '%s\n' "writerA-$i"; done > a.txt ) &
            ( for i in $(seq 1 50); do printf '%s\n' "writerB-$i"; done > b.txt ) &
            wait
            test "$(wc -l < a.txt | tr -d ' ')" = "50"
            test "$(wc -l < b.txt | tr -d ' ')" = "50"

            # 2. 2 MB sequential write past the negotiated 1 MB write size.
            dd if=/dev/zero of=big.bin bs=1048576 count=2 >/dev/null 2>&1
            test "$(wc -c < big.bin | tr -d ' ')" = "2097152"

            # 3. mid-file offset write
            python3 -c "
            with open('seek.bin', 'wb') as f:
                f.write(b'A' * 5000)
            with open('seek.bin', 'r+b') as f:
                f.seek(1000)
                f.write(b'X' * 100)
            with open('seek.bin', 'rb') as f:
                data = f.read()
            assert data[:1000] == b'A' * 1000, 'pre-region overwritten'
            assert data[1000:1100] == b'X' * 100, 'mid-region wrong'
            assert data[1100:] == b'A' * 3900, 'post-region truncated'
            "

            # 4. random-access read at byte 100000 of a multi-MB file
            python3 -c "
            with open('big.bin', 'rb') as f:
                f.seek(100000)
                chunk = f.read(64)
            assert len(chunk) == 64, f'short read: {len(chunk)}'
            "

            # 5. readdir pagination: 200 entries
            mkdir bigdir
            ( cd bigdir && for i in $(seq -w 1 200); do : > "f-$i"; done )
            test "$(ls bigdir | wc -l | tr -d ' ')" = "200"

            # 6. hard link must fail (NFS3ERR_NOTSUPP from proc_link)
            : > target.txt
            if ln target.txt linked.txt 2>/dev/null; then
                echo "hard link unexpectedly succeeded" >&2
                exit 51
            fi
            test ! -e linked.txt
        """).strip()
        shell = subprocess.run(
            ["/bin/sh", "-lc", script],
            cwd=self.target,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)

        checkpoint = self._run_cli("checkpoint", "-m", "protocol edges", cwd=self.target)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stdout + checkpoint.stderr)
        record = mount_registry.read(self.target)
        assert record is not None
        repo = Repository.find(record.repo_path)

        # Coalesced multi-write blob must round-trip byte-exact.
        self.assertEqual(len(repo.read_file("big.bin")), 2 * 1024 * 1024)
        # Offset write produced the right pattern.
        seek_bytes = repo.read_file("seek.bin")
        self.assertEqual(seek_bytes[:1000], b"A" * 1000)
        self.assertEqual(seek_bytes[1000:1100], b"X" * 100)
        self.assertEqual(seek_bytes[1100:], b"A" * 3900)
        # Concurrent writers both landed.
        self.assertEqual(len(repo.read_file("a.txt").splitlines()), 50)
        self.assertEqual(len(repo.read_file("b.txt").splitlines()), 50)

        unmount = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount.returncode, 0, unmount.stdout + unmount.stderr)
        self.assertFalse(_mount_table_contains(self.target))

    async def test_real_mount_posix_semantics(self) -> None:
        """Cases that test POSIX semantics through the kernel client:
        chmod / utimes that must not error (daemon accepts-and-ignores per
        nfs.proc_setattr), atomic rename-over-existing (kernel must end up
        with source content), open-unlinked-fd ("silly rename" trick — kernel
        renames to .nfsXXXX so writes still land), cross-directory rename,
        and long single-component path names."""
        mount = self._run_cli(
            "mount", "--repo", "real-posix", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount.returncode, 0, mount.stdout + mount.stderr)
        self.assertTrue(_mount_table_contains(self.target))

        long_name = "n" + "a" * 240 + ".txt"  # 245 chars; under POSIX_NAME_MAX=255

        script = textwrap.dedent(rf"""
            set -eu
            # 1. chmod must not error. Daemon accepts-and-ignores mode bits;
            #    test that the kernel doesn't surface an error to userspace.
            : > perm.txt
            chmod 600 perm.txt
            chmod 644 perm.txt

            # 2. utimes (touch -t) must not error. Same accept-and-ignore.
            touch -t 200001011200 perm.txt

            # 3. atomic rename-over-existing: source content wins.
            printf 'source-content' > src.txt
            printf 'destination-content' > dst.txt
            mv src.txt dst.txt
            test ! -e src.txt
            test "$(cat dst.txt)" = "source-content"

            # 4. cross-directory rename
            mkdir -p src-dir dst-dir
            printf 'cross-content' > src-dir/file.txt
            mv src-dir/file.txt dst-dir/file.txt
            test ! -e src-dir/file.txt
            test "$(cat dst-dir/file.txt)" = "cross-content"

            # 5. open-unlinked: open a file, unlink it, write more, close.
            #    The kernel client uses silly-rename (rename to .nfsXXXX) so
            #    the inode survives as long as a fd holds it. After close,
            #    the .nfs name disappears too. The DAEMON sees rename + unlink,
            #    not POSIX-style orphaned inodes.
            python3 -c "
            import os
            fd = os.open('orphan.txt', os.O_WRONLY | os.O_CREAT, 0o644)
            os.write(fd, b'before-unlink')
            os.unlink('orphan.txt')
            os.write(fd, b'-after-unlink')
            os.close(fd)
            "
            test ! -e orphan.txt

            # 6. long single-component path (245 chars)
            : > '{long_name}'
            test -e '{long_name}'
        """).strip()
        shell = subprocess.run(
            ["/bin/sh", "-lc", script],
            cwd=self.target,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)

        checkpoint = self._run_cli("checkpoint", "-m", "posix semantics", cwd=self.target)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stdout + checkpoint.stderr)
        record = mount_registry.read(self.target)
        assert record is not None
        repo = Repository.find(record.repo_path)
        self.assertEqual(repo.read_file("dst.txt"), b"source-content")
        self.assertEqual(repo.read_file("dst-dir/file.txt"), b"cross-content")
        self.assertTrue(repo.exists(long_name))
        self.assertFalse(repo.exists("orphan.txt"))

        unmount = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount.returncode, 0, unmount.stdout + unmount.stderr)
        self.assertFalse(_mount_table_contains(self.target))

    async def test_real_mount_perf_floor(self) -> None:
        """Sequential write throughput floor for real macOS localhost NFS."""
        mount = self._run_cli(
            "mount", "--repo", "real-perf", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount.returncode, 0, mount.stdout + mount.stderr)
        self.assertTrue(_mount_table_contains(self.target))

        size_mb = 50
        warmup = subprocess.run(
            ["dd", "if=/dev/zero", f"of={self.target / 'warmup.bin'}", "bs=1048576", "count=8"],
            cwd=self.target,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(warmup.returncode, 0, warmup.stdout + warmup.stderr)

        samples: list[float] = []
        for index in range(3):
            target_path = self.target / f"blob-{index}.bin"
            start = time.monotonic()
            shell = subprocess.run(
                ["dd", "if=/dev/zero", f"of={target_path}", "bs=1048576", f"count={size_mb}"],
                cwd=self.target,
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            elapsed = time.monotonic() - start
            self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)
            self.assertEqual(target_path.stat().st_size, size_mb * 1024 * 1024)
            samples.append(size_mb / elapsed)

        median_mb_s = median(samples)
        self.assertGreaterEqual(
            median_mb_s,
            _MIN_REAL_NFS_MEDIAN_WRITE_MB_S,
            f"50 MB sequential write median throughput too low: "
            f"{median_mb_s:.1f} MB/s from samples {[round(s, 1) for s in samples]}",
        )
        slowest = min(samples)
        fastest = max(samples)
        self.assertGreaterEqual(
            slowest,
            _MIN_REAL_NFS_SAMPLE_WRITE_MB_S,
            f"single-sample throughput fell below floor: "
            f"{slowest:.1f} MB/s from samples {[round(s, 1) for s in samples]}",
        )
        self.assertLessEqual(
            fastest / slowest,
            _MAX_REAL_NFS_SAMPLE_SPREAD,
            f"real NFS write throughput is unstable: "
            f"{[round(s, 1) for s in samples]} MB/s",
        )
        # Always emit the throughput so a passing run still tells us the
        # number. unittest only surfaces stdout on failure unless -b is
        # disabled, but stderr is unbuffered, so we route there.
        sys.stderr.write(
            f"\n  perf: real NFS {size_mb} MB sequential write samples "
            f"{[round(s, 1) for s in samples]} MB/s; median {median_mb_s:.1f} MB/s\n"
        )
        sys.stderr.flush()

        unmount = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount.returncode, 0, unmount.stdout + unmount.stderr)

    async def test_real_mount_remount_cycle(self) -> None:
        """End-to-end durability: mount, write, checkpoint, unmount, remount,
        verify the data is visible from the fresh mount. Confirms the backing
        repo (under TRUNKS_HOME/repos/<safe-repo-name>) survives mount
        teardown — no daemon-local state leaks."""
        mount1 = self._run_cli(
            "mount", "--repo", "real-cycle", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount1.returncode, 0, mount1.stdout + mount1.stderr)
        first_record = mount_registry.read(self.target)
        assert first_record is not None

        shell = subprocess.run(
            ["/bin/sh", "-lc", "echo 'durable content' > durable.txt"],
            cwd=self.target, env=self.env, capture_output=True, text=True,
            check=False, timeout=20,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)

        checkpoint = self._run_cli("checkpoint", "-m", "before remount", cwd=self.target)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stdout + checkpoint.stderr)

        unmount1 = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount1.returncode, 0, unmount1.stdout + unmount1.stderr)
        self.assertFalse(_mount_table_contains(self.target))

        # Wait for daemon to actually exit so the second mount gets a clean slate.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                os.kill(first_record.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)

        # Same repo, same path → should re-attach to the same backing repo
        # data dir and surface the previously committed file.
        mount2 = self._run_cli(
            "mount", "--repo", "real-cycle", "--path", str(self.target), "--mode", "virtual"
        )
        self.assertEqual(mount2.returncode, 0, mount2.stdout + mount2.stderr)
        second_record = mount_registry.read(self.target)
        assert second_record is not None
        self.assertNotEqual(second_record.pid, first_record.pid)

        shell = subprocess.run(
            ["/bin/sh", "-lc", "cat durable.txt"],
            cwd=self.target, env=self.env, capture_output=True, text=True,
            check=False, timeout=20,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)
        self.assertEqual(shell.stdout, "durable content\n")

        unmount2 = self._run_cli("unmount", "--path", str(self.target))
        self.assertEqual(unmount2.returncode, 0, unmount2.stdout + unmount2.stderr)

    def _run_cli(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "trunks.cli", *args],
            cwd=cwd or self.repo_root,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
