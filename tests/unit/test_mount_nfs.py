from __future__ import annotations

import io
import os
import asyncio
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.cli import dispatch
from trunks.mount import nfs, mountd
from trunks.mount.handles import HandleTable
from trunks.mount.nfs import NfsHandler
from trunks.mount.mountd import MountHandler
from trunks.mount.rpc import (
    AUTH_NONE,
    MSG_ACCEPTED,
    MSG_REPLY,
    RpcCall,
    SUCCESS,
)
from trunks.mount.xdr import Packer, Unpacker
from trunks.repository import Repository


def _strip_rpc_header(reply: bytes) -> tuple[int, bytes]:
    """Return (accept_stat, body) from a successful pack_reply_success record."""
    unp = Unpacker(reply)
    unp.unpack_uint32()  # xid
    assert unp.unpack_uint32() == MSG_REPLY
    assert unp.unpack_uint32() == MSG_ACCEPTED
    unp.unpack_uint32()  # verf flavor
    unp.unpack_opaque()  # verf body
    accept_stat = unp.unpack_uint32()
    return accept_stat, reply[unp.offset :]


def _call(
    handler: NfsHandler | MountHandler,
    program: int,
    proc: int,
    args: bytes,
) -> Unpacker:
    call = RpcCall(
        xid=1,
        rpcvers=2,
        prog=program,
        vers=3,
        proc=proc,
        cred_flavor=AUTH_NONE,
        cred_body=b"",
        verf_flavor=AUTH_NONE,
        verf_body=b"",
        args=args,
    )
    reply = handler.dispatch(call)
    accept, body = _strip_rpc_header(reply)
    if accept != SUCCESS:
        raise AssertionError(f"non-success accept_stat: {accept}")
    return Unpacker(body)


class _RepoFixture:
    """Construct a real Repository inside a tempdir for handler tests."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = Path.cwd()

    async def setup(self) -> Repository:
        os.chdir(self.root)
        with redirect_stdout(io.StringIO()):
            await dispatch(["init", "--name", "demo"])
        return Repository.find(self.root)

    def teardown(self) -> None:
        os.chdir(self.cwd)
        self.tmp.cleanup()


class NfsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fix = _RepoFixture()
        self.repo = await self.fix.setup()
        self.handles = HandleTable()
        self.handler = NfsHandler(self.repo, self.handles)

    async def asyncTearDown(self) -> None:
        self.handler.close()
        self.fix.teardown()

    def _root_handle(self) -> bytes:
        return self.handles.root()

    def _lookup(self, dir_handle: bytes, name: str) -> bytes:
        args = Packer()
        args.pack_opaque(dir_handle)
        args.pack_string(name)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_LOOKUP, args.bytes())
        status = unp.unpack_uint32()
        self.assertEqual(status, nfs.NFS3_OK, f"LOOKUP {name} status={status}")
        return unp.unpack_opaque()  # file handle

    def _create_file(self, name: str) -> bytes:
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string(name)
        args.pack_uint32(nfs.UNCHECKED)
        # sattr3 with all "don't change" markers
        for _ in range(3):
            args.pack_bool(False)
        args.pack_bool(False)  # size set
        args.pack_uint32(nfs.DONT_CHANGE)
        args.pack_uint32(nfs.DONT_CHANGE)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_CREATE, args.bytes())
        status = unp.unpack_uint32()
        self.assertEqual(status, nfs.NFS3_OK)
        present = unp.unpack_bool()
        self.assertTrue(present)
        return unp.unpack_opaque()

    async def test_null_proc_returns_empty(self) -> None:
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_NULL, b"")
        self.assertTrue(unp.done())

    async def test_getattr_on_root(self) -> None:
        args = Packer()
        args.pack_opaque(self._root_handle())
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_GETATTR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertEqual(unp.unpack_uint32(), nfs.NF3DIR)
        self.assertEqual(unp.unpack_uint32(), nfs.S_IFDIR | 0o755)

    async def test_lookup_missing_returns_noent(self) -> None:
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string("ghost.md")
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_LOOKUP, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3ERR_NOENT)

    async def test_create_rejects_path_traversal_names(self) -> None:
        for name in ("..", "a/b", "a\\b", "bad\x00name", "bad\x1fname"):
            args = Packer()
            args.pack_opaque(self._root_handle())
            args.pack_string(name)
            args.pack_uint32(nfs.UNCHECKED)
            for _ in range(3):
                args.pack_bool(False)
            args.pack_bool(False)
            args.pack_uint32(nfs.DONT_CHANGE)
            args.pack_uint32(nfs.DONT_CHANGE)
            unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_CREATE, args.bytes())
            self.assertEqual(unp.unpack_uint32(), nfs.NFS3ERR_NAMETOOLONG, name)
        self.assertEqual(self.repo.index_entries(), [])

    async def test_forged_deterministic_path_handle_is_stale(self) -> None:
        import hashlib

        forged = hashlib.sha256(b"secret.txt").digest()[:32]
        args = Packer()
        args.pack_opaque(forged)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_GETATTR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3ERR_STALE)

    async def test_lookup_dot_entries(self) -> None:
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string(".")
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_LOOKUP, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertEqual(unp.unpack_opaque(), self._root_handle())

        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string("src")
        args.pack_uint32(nfs.UNCHECKED)
        for _ in range(3):
            args.pack_bool(False)
        args.pack_bool(False)
        args.pack_uint32(nfs.DONT_CHANGE)
        args.pack_uint32(nfs.DONT_CHANGE)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_MKDIR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertTrue(unp.unpack_bool())
        src_handle = unp.unpack_opaque()

        args = Packer()
        args.pack_opaque(src_handle)
        args.pack_string("..")
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_LOOKUP, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertEqual(unp.unpack_opaque(), self._root_handle())

    async def test_access_grants_requested_bits(self) -> None:
        requested = (
            nfs.ACCESS3_READ
            | nfs.ACCESS3_LOOKUP
            | nfs.ACCESS3_MODIFY
            | nfs.ACCESS3_EXTEND
            | nfs.ACCESS3_DELETE
            | nfs.ACCESS3_EXECUTE
        )
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_uint32(requested)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_ACCESS, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        if unp.unpack_bool():
            self._consume_fattr(unp)
        self.assertEqual(unp.unpack_uint32(), requested)

    async def test_create_then_lookup_roundtrip(self) -> None:
        created = self._create_file("alpha.md")
        looked_up = self._lookup(self._root_handle(), "alpha.md")
        self.assertEqual(created, looked_up)

    async def test_write_then_read_roundtrip(self) -> None:
        handle = self._create_file("notes.md")
        payload = b"hello, trunks!"
        # WRITE
        args = Packer()
        args.pack_opaque(handle)
        args.pack_uint64(0)
        args.pack_uint32(len(payload))
        args.pack_uint32(nfs.FILE_SYNC)
        args.pack_opaque(payload)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_WRITE, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self._consume_wcc(unp)
        self.assertEqual(unp.unpack_uint32(), len(payload))
        self.assertEqual(unp.unpack_uint32(), nfs.UNSTABLE)
        self.assertEqual(unp.unpack_fopaque(8), nfs.WRITE_VERIFIER)

        # COMMIT — the daemon defers backend writes until COMMIT, matching the
        # NFSv3 contract (the kernel issues COMMIT after fsync()/close()).
        # Without it, the write stays in the dirty buffer and the backend is
        # still empty — invisible to NFS READ via the buffer, but invisible
        # to repo.read_file too.
        commit_args = Packer()
        commit_args.pack_opaque(handle)
        commit_args.pack_uint64(0)
        commit_args.pack_uint32(len(payload))
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_COMMIT, commit_args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertEqual(self.repo.read_file("notes.md"), payload)

        # READ
        args = Packer()
        args.pack_opaque(handle)
        args.pack_uint64(0)
        args.pack_uint32(1024)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_READ, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        # post_op_attr: present? then fattr3 (84 bytes after the bool)
        if unp.unpack_bool():
            # consume fattr3 — 21 uint32-equivalents (with two uint64 size/used and three nfstime3 pairs).
            # Easier: use field layout.
            for _ in range(2):
                unp.unpack_uint32()  # type, mode
            unp.unpack_uint32()  # nlink
            unp.unpack_uint32()  # uid
            unp.unpack_uint32()  # gid
            unp.unpack_uint64()  # size
            unp.unpack_uint64()  # used
            unp.unpack_uint32()  # rdev1
            unp.unpack_uint32()  # rdev2
            unp.unpack_uint64()  # fsid
            unp.unpack_uint64()  # fileid
            for _ in range(6):
                unp.unpack_uint32()  # 3x nfstime3 (sec, nsec)
        count = unp.unpack_uint32()
        eof = unp.unpack_bool()
        data = unp.unpack_opaque()
        self.assertEqual(count, len(payload))
        self.assertTrue(eof)
        self.assertEqual(data, payload)

    async def test_large_write_stages_blob_before_commit_fences_index(self) -> None:
        handle = self._create_file("large.bin")
        payload = b"x" * 16

        with (
            patch.object(nfs, "BACKGROUND_STAGE_MIN_BYTES", 4),
            patch.object(nfs, "BACKGROUND_STAGE_DELAY_SECONDS", 0),
        ):
            args = Packer()
            args.pack_opaque(handle)
            args.pack_uint64(0)
            args.pack_uint32(len(payload))
            args.pack_uint32(nfs.UNSTABLE)
            args.pack_opaque(payload)
            unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_WRITE, args.bytes())
            self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
            await asyncio.sleep(0)

            future = self.handler._stage_futures.get("large.bin")
            self.assertIsNotNone(future)
            assert future is not None
            staged = future.result(timeout=5)
            self.assertEqual(self.repo.blob_payload(staged.oid), payload)
            self.assertEqual(self.repo.read_file("large.bin"), b"")

            commit_args = Packer()
            commit_args.pack_opaque(handle)
            commit_args.pack_uint64(0)
            commit_args.pack_uint32(len(payload))
            unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_COMMIT, commit_args.bytes())
            self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
            self.assertEqual(self.repo.read_file("large.bin"), payload)

    async def test_macos_appledouble_sidecars_are_ephemeral(self) -> None:
        self._create_file("hello.txt")
        sidecar = self._create_file("._hello.txt")
        payload = b"metadata"

        args = Packer()
        args.pack_opaque(sidecar)
        args.pack_uint64(0)
        args.pack_uint32(len(payload))
        args.pack_uint32(nfs.FILE_SYNC)
        args.pack_opaque(payload)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_WRITE, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self._consume_wcc(unp)
        self.assertEqual(unp.unpack_uint32(), len(payload))
        self.assertEqual(unp.unpack_uint32(), nfs.UNSTABLE)
        self.assertEqual(unp.unpack_fopaque(8), nfs.WRITE_VERIFIER)

        self.assertFalse(self.repo.exists("._hello.txt"))

        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_uint64(0)
        args.pack_fopaque(b"\x00" * 8, 8)
        args.pack_uint32(8192)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_READDIR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        if unp.unpack_bool():
            self._consume_fattr(unp)
        unp.unpack_fopaque(8)
        names: list[str] = []
        while unp.unpack_bool():
            unp.unpack_uint64()
            names.append(unp.unpack_string())
            unp.unpack_uint64()
        self.assertEqual(names, [".", "..", "hello.txt"])
        self.assertNotIn("._hello.txt", names)

    async def test_readdir_lists_children(self) -> None:
        self._create_file("a.md")
        self._create_file("b.md")
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_uint64(0)
        args.pack_fopaque(b"\x00" * 8, 8)
        args.pack_uint32(8192)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_READDIR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        # Skip post_op_attr
        if unp.unpack_bool():
            self._consume_fattr(unp)
        unp.unpack_fopaque(8)  # cookieverf
        names: list[str] = []
        while unp.unpack_bool():
            unp.unpack_uint64()  # fileid
            names.append(unp.unpack_string())
            unp.unpack_uint64()  # cookie
        eof = unp.unpack_bool()
        self.assertTrue(eof)
        self.assertEqual(names[:2], [".", ".."])
        self.assertEqual(sorted(names[2:]), ["a.md", "b.md"])

    async def test_readdirplus_lists_dot_entries(self) -> None:
        self._create_file("a.md")
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_uint64(0)
        args.pack_fopaque(b"\x00" * 8, 8)
        args.pack_uint32(8192)
        args.pack_uint32(8192)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_READDIRPLUS, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        if unp.unpack_bool():
            self._consume_fattr(unp)
        unp.unpack_fopaque(8)
        names: list[str] = []
        while unp.unpack_bool():
            unp.unpack_uint64()
            names.append(unp.unpack_string())
            unp.unpack_uint64()
            if unp.unpack_bool():
                self._consume_fattr(unp)
            if unp.unpack_bool():
                unp.unpack_opaque()
        self.assertTrue(unp.unpack_bool())
        self.assertEqual(names[:2], [".", ".."])
        self.assertIn("a.md", names)

    async def test_readdirplus_reuses_namespace_cache(self) -> None:
        for index in range(50):
            self.repo.write_file(f"src/file-{index:03d}.txt", b"x")

        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_uint64(0)
        args.pack_fopaque(b"\x00" * 8, 8)
        args.pack_uint32(8192)
        args.pack_uint32(8192)

        with patch.object(self.repo, "index_entries", wraps=self.repo.index_entries) as index_entries:
            for _ in range(2):
                unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_READDIRPLUS, args.bytes())
                self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)

        self.assertEqual(index_entries.call_count, 1)

    async def test_remove_deletes_file(self) -> None:
        self._create_file("doomed.md")
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string("doomed.md")
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_REMOVE, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertFalse(self.repo.exists("doomed.md"))

    async def test_mkdir_creates_directory(self) -> None:
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string("subdir")
        # sattr3 all-defaults
        for _ in range(3):
            args.pack_bool(False)
        args.pack_bool(False)
        args.pack_uint32(nfs.DONT_CHANGE)
        args.pack_uint32(nfs.DONT_CHANGE)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_MKDIR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertTrue(unp.unpack_bool())
        unp.unpack_opaque()  # handle
        # The directory exists once a marker file exists under it.
        self.assertTrue(self.repo.exists("subdir"))

    async def test_rename_moves_file(self) -> None:
        self._create_file("from.md")
        self.repo.write_file("from.md", b"contents")
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string("from.md")
        args.pack_opaque(self._root_handle())
        args.pack_string("to.md")
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_RENAME, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertFalse(self.repo.exists("from.md"))
        self.assertEqual(self.repo.read_file("to.md"), b"contents")

    async def test_setattr_truncate_shrinks_file(self) -> None:
        handle = self._create_file("trim.md")
        self.repo.write_file("trim.md", b"abcdefghij")
        args = Packer()
        args.pack_opaque(handle)
        # mode/uid/gid set-flags = false
        for _ in range(3):
            args.pack_bool(False)
        args.pack_bool(True)  # size set
        args.pack_uint64(4)
        args.pack_uint32(nfs.DONT_CHANGE)  # atime
        args.pack_uint32(nfs.DONT_CHANGE)  # mtime
        args.pack_bool(False)  # no guard
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_SETATTR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3_OK)
        self.assertEqual(self.repo.read_file("trim.md"), b"abcd")

    async def test_stale_handle_returns_stale(self) -> None:
        bogus = b"\xff" * 32
        args = Packer()
        args.pack_opaque(bogus)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_GETATTR, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3ERR_STALE)

    async def test_unsupported_proc_returns_proc_unavail(self) -> None:
        args = Packer()
        args.pack_opaque(self._root_handle())
        args.pack_string("link")
        for _ in range(3):
            args.pack_bool(False)
        args.pack_bool(False)  # size
        args.pack_uint32(nfs.DONT_CHANGE)  # atime
        args.pack_uint32(nfs.DONT_CHANGE)  # mtime
        args.pack_string("target")
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_SYMLINK, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3ERR_NOTSUPP)
        self.assertTrue(unp.unpack_bool())  # dir_wcc.before

    async def test_readlink_unsupported_returns_post_op_attr(self) -> None:
        self.repo.write_file("plain.txt", b"plain")
        handle = self.handler.handles.register("plain.txt")
        args = Packer()
        args.pack_opaque(handle)
        unp = _call(self.handler, nfs.PROGRAM, nfs.NFSPROC3_READLINK, args.bytes())
        self.assertEqual(unp.unpack_uint32(), nfs.NFS3ERR_NOTSUPP)
        self.assertTrue(unp.unpack_bool())

    def _consume_fattr(self, unp: Unpacker) -> None:
        for _ in range(5):
            unp.unpack_uint32()
        unp.unpack_uint64()
        unp.unpack_uint64()
        unp.unpack_uint32()
        unp.unpack_uint32()
        unp.unpack_uint64()
        unp.unpack_uint64()
        for _ in range(6):
            unp.unpack_uint32()

    def _consume_wcc(self, unp: Unpacker) -> None:
        if unp.unpack_bool():
            unp.unpack_uint64()
            for _ in range(4):
                unp.unpack_uint32()
        if unp.unpack_bool():
            self._consume_fattr(unp)


class MountHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.handles = HandleTable()
        self.handler = MountHandler("demo", self.handles)

    async def test_null_proc(self) -> None:
        unp = _call(self.handler, mountd.PROGRAM, mountd.MOUNTPROC3_NULL, b"")
        self.assertTrue(unp.done())

    async def test_mnt_returns_root_handle(self) -> None:
        args = Packer()
        args.pack_string("/demo")
        unp = _call(self.handler, mountd.PROGRAM, mountd.MOUNTPROC3_MNT, args.bytes())
        self.assertEqual(unp.unpack_uint32(), mountd.MNT3_OK)
        handle = unp.unpack_opaque()
        self.assertEqual(handle, self.handles.root())
        flavor_count = unp.unpack_uint32()
        self.assertGreaterEqual(flavor_count, 1)

    async def test_mnt_unknown_export_returns_noent(self) -> None:
        args = Packer()
        args.pack_string("/other")
        unp = _call(self.handler, mountd.PROGRAM, mountd.MOUNTPROC3_MNT, args.bytes())
        self.assertEqual(unp.unpack_uint32(), mountd.MNT3ERR_NOENT)

    async def test_mnt_can_be_disabled_after_kernel_mount(self) -> None:
        self.handler.disable_mnt()
        args = Packer()
        args.pack_string("/demo")
        unp = _call(self.handler, mountd.PROGRAM, mountd.MOUNTPROC3_MNT, args.bytes())
        self.assertEqual(unp.unpack_uint32(), mountd.MNT3ERR_ACCES)


if __name__ == "__main__":
    unittest.main()
