"""NFSv3 protocol (RFC 1813) over a Trunks Repository.

This module implements the NFSv3 procedures we need so the OS NFS client can
treat a Trunks repo as a normal filesystem. Reads pull bytes through
`Repository.read_file`; writes flow into `Repository.write_file`. Listing,
rename, mkdir, remove and friends round-trip through the same SQLite index, so
`trunks checkpoint` afterwards turns the live edits into a real commit.
"""

from __future__ import annotations

import os
import time
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from ..errors import ObjectNotFound
from ..ids import ObjectId
from ..index import IndexEntry
from ..repository import Repository
from .handles import HandleTable
from .rpc import RpcCall, pack_reply_accepted_error, pack_reply_success, GARBAGE_ARGS, PROC_UNAVAIL
from .xdr import Packer, Unpacker, XdrError

def _process_owner() -> tuple[int, int]:
    if os.name == "nt":
        return 0, 0
    return os.geteuid(), os.getegid()


# Single-user localhost NFS: report files as owned by the daemon's own user so
# the kernel NFS client (running as the same user) is allowed to write to them.
_OWNER_UID, _OWNER_GID = _process_owner()

PROGRAM = 100003
VERSION = 3

# Procedure numbers
NFSPROC3_NULL = 0
NFSPROC3_GETATTR = 1
NFSPROC3_SETATTR = 2
NFSPROC3_LOOKUP = 3
NFSPROC3_ACCESS = 4
NFSPROC3_READLINK = 5
NFSPROC3_READ = 6
NFSPROC3_WRITE = 7
NFSPROC3_CREATE = 8
NFSPROC3_MKDIR = 9
NFSPROC3_SYMLINK = 10
NFSPROC3_MKNOD = 11
NFSPROC3_REMOVE = 12
NFSPROC3_RMDIR = 13
NFSPROC3_RENAME = 14
NFSPROC3_LINK = 15
NFSPROC3_READDIR = 16
NFSPROC3_READDIRPLUS = 17
NFSPROC3_FSSTAT = 18
NFSPROC3_FSINFO = 19
NFSPROC3_PATHCONF = 20
NFSPROC3_COMMIT = 21

# Status codes (subset)
NFS3_OK = 0
NFS3ERR_PERM = 1
NFS3ERR_NOENT = 2
NFS3ERR_IO = 5
NFS3ERR_ACCES = 13
NFS3ERR_EXIST = 17
NFS3ERR_NOTDIR = 20
NFS3ERR_ISDIR = 21
NFS3ERR_INVAL = 22
NFS3ERR_NAMETOOLONG = 63
NFS3ERR_NOTEMPTY = 66
NFS3ERR_STALE = 70
NFS3ERR_NOTSUPP = 10004
NFS3ERR_BADHANDLE = 10001
NFS3ERR_SERVERFAULT = 10006

# File types
NF3REG = 1
NF3DIR = 2
NF3BLK = 3
NF3CHR = 4
NF3LNK = 5
NF3SOCK = 6
NF3FIFO = 7

# POSIX type bits OR'd into mode3. RFC 1813 §2.5 mode3 references RFC 1094
# §2.3.5 which encodes file type in the high bits of mode. macOS / xnu's NFS
# client uses these bits — when only permission bits are returned the kernel
# treats the vnode as type=0 and surfaces every open() / readdir() with EPERM
# even after ACCESS3 grants 0x3f.
S_IFREG = 0o100000
S_IFDIR = 0o040000
S_IFLNK = 0o120000

# Access bits (RFC 1813 §3.3.4)
ACCESS3_READ = 0x0001
ACCESS3_LOOKUP = 0x0002
ACCESS3_MODIFY = 0x0004
ACCESS3_EXTEND = 0x0008
ACCESS3_DELETE = 0x0010
ACCESS3_EXECUTE = 0x0020

# Stable_how (WRITE)
UNSTABLE = 0
DATA_SYNC = 1
FILE_SYNC = 2

# create mode (CREATE)
UNCHECKED = 0
GUARDED = 1
EXCLUSIVE = 2

# sattr3 set-it flags (SETATTR)
DONT_CHANGE = 0
SET_TO_CLIENT_TIME = 1
SET_TO_SERVER_TIME = 2

FSID = 0xC0FEE5
ROOT_FILEID = 1
DIR_SIZE = 4096
WRITE_VERIFIER = b"trunks01"
BACKGROUND_STAGE_MIN_BYTES = 64 << 20
BACKGROUND_STAGE_DELAY_SECONDS = 0.05
BACKGROUND_STAGE_WORKERS = 2


@dataclass(frozen=True)
class _Attr:
    type: int
    mode: int
    nlink: int
    size: int
    fileid: int
    mtime: float


@dataclass(frozen=True)
class _StagedBlob:
    generation: int
    oid: ObjectId
    size: int


class NfsHandler:
    """Dispatches NFSv3 procedures against a Trunks Repository."""

    def __init__(self, repo: Repository, handles: HandleTable) -> None:
        self.repo = repo
        self.handles = handles
        # Use a sane non-zero timestamp for entries that have not been touched
        # through this daemon yet.
        self._epoch = time.time()
        self._mtimes: dict[str, float] = {}
        self._entries_cache: list[IndexEntry] | None = None
        self._files_cache: set[str] | None = None
        self._dirs_cache: set[str] | None = None
        self._children_cache: dict[str, set[str]] | None = None
        self._size_cache: dict[str, int] = {}
        self._ephemeral_files: dict[str, bytes] = {}
        self._write_buffers: dict[str, bytearray] = {}
        self._dirty_buffers: set[str] = set()
        self._dirty_generations: dict[str, int] = {}
        self._stage_futures: dict[str, Future[_StagedBlob]] = {}
        self._stage_generations: dict[str, int] = {}
        self._stage_timers: dict[str, asyncio.TimerHandle] = {}
        self._stage_executor = ThreadPoolExecutor(
            max_workers=BACKGROUND_STAGE_WORKERS,
            thread_name_prefix="trunks-nfs-stage",
        )
        # Register the root path so initial GETATTR can resolve it.
        self.handles.register("")

    def close(self) -> None:
        for timer in self._stage_timers.values():
            timer.cancel()
        self._stage_timers.clear()
        for path in list(self._dirty_buffers):
            self._flush_path(path)
        self._stage_executor.shutdown(wait=True)

    def dispatch(self, call: RpcCall) -> bytes:
        try:
            handler = _PROCS.get(call.proc)
            if handler is None:
                return pack_reply_accepted_error(call.xid, PROC_UNAVAIL)
            try:
                body = handler(self, Unpacker(call.args))
            except XdrError:
                return pack_reply_accepted_error(call.xid, GARBAGE_ARGS)
            return pack_reply_success(call.xid, body)
        except Exception:  # noqa: BLE001 — guard the event loop; reply is the recovery path
            return pack_reply_success(call.xid, _status(NFS3ERR_SERVERFAULT))

    # ---- Procedure handlers ----

    def proc_null(self, _: Unpacker) -> bytes:
        return b""

    def proc_getattr(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        if path is None:
            return _status(NFS3ERR_STALE)
        attr = self._stat(path)
        if attr is None:
            return _status(NFS3ERR_NOENT)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_fattr(out, attr)
        return out.bytes()

    def proc_setattr(self, unp: Unpacker) -> bytes:
        # SETATTR is the simplest "approve and ignore" for our purposes —
        # mode/uid/gid/atime/mtime aren't tracked, but truncation is.
        path = self._resolve_handle(unp)
        if path is None:
            return _status(NFS3ERR_STALE)

        # sattr3
        _read_set_uint32(unp)  # mode
        _read_set_uint32(unp)  # uid
        _read_set_uint32(unp)  # gid
        size_set = unp.unpack_bool()
        new_size = unp.unpack_uint64() if size_set else None
        _read_set_time(unp)  # atime
        _read_set_time(unp)  # mtime
        # guard (sattrguard3)
        check = unp.unpack_bool()
        if check:
            _read_nfstime(unp)

        before = self._stat(path)
        if before is None or before.type == NF3DIR:
            # Directories can't have their size changed; everything else we accept.
            if new_size is not None:
                return _wcc_error(NFS3ERR_INVAL, before)

        if new_size is not None:
            data = self._read_bytes(path) if before and before.type == NF3REG else b""
            if new_size < len(data):
                data = data[:new_size]
            elif new_size > len(data):
                data = data + b"\x00" * (new_size - len(data))
            self._write_bytes(path, data)
            self._mtimes[path] = time.time()

        after = self._stat(path)
        return _wcc_ok(before, after)

    def proc_lookup(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        if dir_path is None:
            return _status(NFS3ERR_STALE)
        name = unp.unpack_string()
        dir_attr = self._stat(dir_path)
        if dir_attr is None or dir_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        # macOS NFS verifies "." and ".." after READDIRPLUS — failing to resolve
        # them here makes the kernel mark the directory as inconsistent and
        # surface every subsequent op as EPERM.
        if name == ".":
            target = dir_path
        elif name == "..":
            target = "" if dir_path == "" else (dir_path.rsplit("/", 1)[0] if "/" in dir_path else "")
        else:
            if not _name_ok(name):
                return _wcc_error(NFS3ERR_NAMETOOLONG, dir_attr)
            target = _join(dir_path, name)
        target_attr = self._stat(target)
        if target_attr is None:
            out = Packer()
            out.pack_uint32(NFS3ERR_NOENT)
            _pack_post_op_attr(out, dir_attr)
            return out.bytes()
        handle = self.handles.register(target)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_handle(out, handle)
        _pack_post_op_attr(out, target_attr)
        _pack_post_op_attr(out, dir_attr)
        return out.bytes()

    def proc_access(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        if path is None:
            return _status(NFS3ERR_STALE)
        requested = unp.unpack_uint32()
        attr = self._stat(path)
        if attr is None:
            return _status(NFS3ERR_NOENT)
        # Localhost single-user mount: the kernel NFS client running as the
        # daemon's own user is the trust boundary. Grant every bit it asks
        # for — including ACCESS3_EXECUTE on directories, which macOS treats
        # as "you may open files inside this directory" and which RFC 1813
        # leaves unspecified for non-files. Returning a strict subset (e.g.
        # withholding EXECUTE on a dir) makes macOS open(O_CREAT) fail with
        # EPERM client-side without ever issuing CREATE3.
        granted = requested
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_post_op_attr(out, attr)
        out.pack_uint32(granted)
        return out.bytes()

    def proc_readlink(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        attr = self._stat(path) if path is not None else None
        out = Packer()
        out.pack_uint32(NFS3ERR_NOTSUPP)
        _pack_post_op_attr(out, attr)
        return out.bytes()

    def proc_read(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        if path is None:
            return _status(NFS3ERR_STALE)
        offset = unp.unpack_uint64()
        count = unp.unpack_uint32()
        attr = self._stat(path)
        if attr is None:
            return _status(NFS3ERR_NOENT)
        if attr.type == NF3DIR:
            return _wcc_error(NFS3ERR_ISDIR, attr)
        try:
            data = self._read_bytes(path)
        except ObjectNotFound:
            return _status(NFS3ERR_NOENT)
        slice_ = data[offset : offset + count]
        eof = (offset + len(slice_)) >= len(data)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_post_op_attr(out, attr)
        out.pack_uint32(len(slice_))
        out.pack_bool(eof)
        out.pack_opaque(slice_)
        return out.bytes()

    def proc_write(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        if path is None:
            return _status(NFS3ERR_STALE)
        offset = unp.unpack_uint64()
        count = unp.unpack_uint32()
        unp.unpack_uint32()  # requested stable_how
        data = unp.unpack_opaque()
        if count != len(data):
            return _wcc_error(NFS3ERR_INVAL, self._stat(path))
        before = self._stat(path)
        if before is None:
            return _wcc_error(NFS3ERR_NOENT, None)
        if before.type == NF3DIR:
            return _wcc_error(NFS3ERR_ISDIR, before)
        # Reuse the existing dirty buffer when one is live: macOS NFS splits a
        # multi-MB write into ~32 KB RPCs, so allocating a fresh bytearray per
        # call would make sequential writes O(N²) in file size. Mutating the
        # existing bytearray keeps the per-RPC cost proportional to the chunk.
        merged = self._write_buffers.get(path)
        if merged is None:
            if path in self._ephemeral_files:
                merged = bytearray(self._ephemeral_files[path])
            else:
                merged = bytearray(self.repo.read_file(path))
        if offset > len(merged):
            merged.extend(b"\x00" * (offset - len(merged)))
        end = offset + len(data)
        if end > len(merged):
            merged.extend(b"\x00" * (end - len(merged)))
        merged[offset:end] = data
        self._write_buffers[path] = merged
        self._dirty_buffers.add(path)
        self._dirty_generations[path] = self._dirty_generations.get(path, 0) + 1
        self._size_cache[path] = len(merged)
        # Stage large blobs in the background while WRITE traffic is still
        # flowing. Staging inserts immutable blob objects only; COMMIT is the
        # fence that updates the index to the latest staged object.
        self._maybe_stage_background(path)
        self._mtimes[path] = time.time()
        after = self._stat(path)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_wcc(out, before, after)
        out.pack_uint32(len(data))
        out.pack_uint32(UNSTABLE)
        out.pack_fopaque(WRITE_VERIFIER, 8)
        return out.bytes()

    def proc_create(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        if dir_path is None:
            return _status(NFS3ERR_STALE)
        name = unp.unpack_string()
        mode = unp.unpack_uint32()
        if mode == EXCLUSIVE:
            unp.unpack_fopaque(8)  # createverf3
        else:
            _read_sattr(unp)
        dir_attr = self._stat(dir_path)
        if dir_attr is None or dir_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        if not _name_ok(name):
            return _wcc_error(NFS3ERR_NAMETOOLONG, dir_attr)
        target = _join(dir_path, name)
        existing = self._stat(target)
        if existing is not None and mode == GUARDED:
            return _wcc_error(NFS3ERR_EXIST, dir_attr)
        if existing is None:
            self._write_bytes(target, b"")
        self._mtimes[target] = time.time()
        self._mtimes[dir_path] = time.time()
        handle = self.handles.register(target)
        target_attr = self._stat(target)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        out.pack_bool(True)
        _pack_handle(out, handle)
        _pack_post_op_attr(out, target_attr)
        _pack_wcc(out, dir_attr, self._stat(dir_path))
        return out.bytes()

    def proc_mkdir(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        if dir_path is None:
            return _status(NFS3ERR_STALE)
        name = unp.unpack_string()
        _read_sattr(unp)
        dir_attr = self._stat(dir_path)
        if dir_attr is None or dir_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        if not _name_ok(name):
            return _wcc_error(NFS3ERR_NAMETOOLONG, dir_attr)
        target = _join(dir_path, name)
        if self._stat(target) is not None:
            return _wcc_error(NFS3ERR_EXIST, dir_attr)
        # Materialize the directory by laying down a sentinel marker file. The
        # marker is hidden from list_dir output via the .gitkeep convention so
        # `trunks checkpoint` captures the empty dir without surfacing it.
        marker = _join(target, ".trunkskeep")
        self.repo.write_file(marker, b"")
        self._invalidate_namespace(target)
        self._mtimes[target] = time.time()
        self._mtimes[dir_path] = time.time()
        handle = self.handles.register(target)
        target_attr = self._stat(target)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        out.pack_bool(True)
        _pack_handle(out, handle)
        _pack_post_op_attr(out, target_attr)
        _pack_wcc(out, dir_attr, self._stat(dir_path))
        return out.bytes()

    def proc_symlink(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        name = unp.unpack_string()
        _read_sattr(unp)
        unp.unpack_string()  # symlink_data
        dir_attr = self._stat(dir_path) if dir_path is not None and _name_ok(name) else None
        return _wcc_error(NFS3ERR_NOTSUPP, dir_attr)

    def proc_mknod(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        name = unp.unpack_string()
        mknod_type = unp.unpack_uint32()
        if mknod_type in {NF3CHR, NF3BLK}:
            _read_sattr(unp)
            unp.unpack_uint32()  # specdata1
            unp.unpack_uint32()  # specdata2
        elif mknod_type in {NF3SOCK, NF3FIFO}:
            _read_sattr(unp)
        dir_attr = self._stat(dir_path) if dir_path is not None and _name_ok(name) else None
        return _wcc_error(NFS3ERR_NOTSUPP, dir_attr)

    def proc_remove(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        if dir_path is None:
            return _status(NFS3ERR_STALE)
        name = unp.unpack_string()
        dir_attr = self._stat(dir_path)
        if dir_attr is None or dir_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        target = _join(dir_path, name)
        target_attr = self._stat(target)
        if target_attr is None:
            return _wcc_error(NFS3ERR_NOENT, dir_attr)
        if target_attr.type == NF3DIR:
            return _wcc_error(NFS3ERR_ISDIR, dir_attr)
        self._delete_bytes(target)
        self.handles.forget(target)
        self._mtimes.pop(target, None)
        self._mtimes[dir_path] = time.time()
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_wcc(out, dir_attr, self._stat(dir_path))
        return out.bytes()

    def proc_rmdir(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        if dir_path is None:
            return _status(NFS3ERR_STALE)
        name = unp.unpack_string()
        dir_attr = self._stat(dir_path)
        if dir_attr is None or dir_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        target = _join(dir_path, name)
        target_attr = self._stat(target)
        if target_attr is None:
            return _wcc_error(NFS3ERR_NOENT, dir_attr)
        if target_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        children = self._children(target)
        non_marker = [c for c in children if c != ".trunkskeep"]
        if non_marker:
            return _wcc_error(NFS3ERR_NOTEMPTY, dir_attr)
        # Drop the marker (and any other files) so the directory disappears.
        for child in self._all_files_under(target):
            self._delete_bytes(child)
            self.handles.forget(child)
        self._invalidate_namespace(target)
        self.handles.forget(target)
        self._mtimes.pop(target, None)
        self._mtimes[dir_path] = time.time()
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_wcc(out, dir_attr, self._stat(dir_path))
        return out.bytes()

    def proc_rename(self, unp: Unpacker) -> bytes:
        from_dir = self._resolve_handle(unp)
        if from_dir is None:
            return _status(NFS3ERR_STALE)
        from_name = unp.unpack_string()
        to_dir = self._resolve_handle(unp)
        if to_dir is None:
            return _status(NFS3ERR_STALE)
        to_name = unp.unpack_string()
        from_attr = self._stat(from_dir)
        to_attr = self._stat(to_dir)
        if from_attr is None or from_attr.type != NF3DIR:
            return _rename_wcc_error(NFS3ERR_NOTDIR, from_attr, to_attr)
        if to_attr is None or to_attr.type != NF3DIR:
            return _rename_wcc_error(NFS3ERR_NOTDIR, from_attr, to_attr)
        if not (_name_ok(from_name) and _name_ok(to_name)):
            return _rename_wcc_error(NFS3ERR_NAMETOOLONG, from_attr, to_attr)
        source = _join(from_dir, from_name)
        dest = _join(to_dir, to_name)
        source_attr = self._stat(source)
        if source_attr is None:
            return _rename_wcc_error(NFS3ERR_NOENT, from_attr, to_attr)
        if source_attr.type == NF3DIR:
            self._move_directory(source, dest)
        elif self._is_ephemeral_path(source) or self._is_ephemeral_path(dest):
            data = self._read_bytes(source)
            self._delete_bytes(source)
            self._write_bytes(dest, data)
            self.handles.rename(source, dest)
            self._mtimes.pop(source, None)
            self._mtimes[dest] = time.time()
        else:
            self._flush_path(source)
            try:
                self.repo.move_file(source, dest)
            except ObjectNotFound:
                return _rename_wcc_error(NFS3ERR_NOENT, from_attr, to_attr)
            self.handles.rename(source, dest)
            self._invalidate_namespace(source)
            self._invalidate_namespace(dest)
            self._mtimes.pop(source, None)
            self._mtimes[dest] = time.time()
        self._mtimes[from_dir] = time.time()
        self._mtimes[to_dir] = time.time()
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_wcc(out, from_attr, self._stat(from_dir))
        _pack_wcc(out, to_attr, self._stat(to_dir))
        return out.bytes()

    def proc_link(self, unp: Unpacker) -> bytes:
        file_path = self._resolve_handle(unp)
        link_dir = self._resolve_handle(unp)
        unp.unpack_string()
        file_attr = self._stat(file_path)
        dir_attr = self._stat(link_dir)
        out = Packer()
        out.pack_uint32(NFS3ERR_NOTSUPP)
        _pack_post_op_attr(out, file_attr)
        _pack_wcc(out, dir_attr, dir_attr)
        return out.bytes()

    def proc_readdir(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        if dir_path is None:
            return _status(NFS3ERR_STALE)
        cookie = unp.unpack_uint64()
        unp.unpack_fopaque(8)  # cookieverf3
        unp.unpack_uint32()  # count
        dir_attr = self._stat(dir_path)
        if dir_attr is None or dir_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        entries = self._dir_entries(dir_path, dir_attr)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_post_op_attr(out, dir_attr)
        out.pack_fopaque(b"\x00" * 8, 8)  # cookieverf3
        index = int(cookie)
        for offset, (name, fileid, _path) in enumerate(entries[index:], start=index + 1):
            out.pack_bool(True)
            out.pack_uint64(fileid)
            out.pack_string(name)
            out.pack_uint64(offset)
        out.pack_bool(False)
        out.pack_bool(True)  # eof
        return out.bytes()

    def proc_readdirplus(self, unp: Unpacker) -> bytes:
        dir_path = self._resolve_handle(unp)
        if dir_path is None:
            return _status(NFS3ERR_STALE)
        cookie = unp.unpack_uint64()
        unp.unpack_fopaque(8)  # cookieverf3
        unp.unpack_uint32()  # dircount
        unp.unpack_uint32()  # maxcount
        dir_attr = self._stat(dir_path)
        if dir_attr is None or dir_attr.type != NF3DIR:
            return _wcc_error(NFS3ERR_NOTDIR, dir_attr)
        entries = self._dir_entries(dir_path, dir_attr)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_post_op_attr(out, dir_attr)
        out.pack_fopaque(b"\x00" * 8, 8)
        index = int(cookie)
        for offset, (name, fileid, child_path) in enumerate(entries[index:], start=index + 1):
            child_attr = self._stat(child_path)
            handle = self.handles.register(child_path) if child_path != dir_path else self.handles.register(child_path)
            out.pack_bool(True)
            out.pack_uint64(fileid)
            out.pack_string(name)
            out.pack_uint64(offset)
            _pack_post_op_attr(out, child_attr)
            _pack_post_op_handle(out, handle)
        out.pack_bool(False)
        out.pack_bool(True)
        return out.bytes()

    def _dir_entries(self, dir_path: str, dir_attr: _Attr) -> list[tuple[str, int, str]]:
        """Return [(name, fileid, path)] including synthetic '.' and '..'.

        macOS NFS client treats a directory whose readdir reply lacks '.' and '..'
        as broken and surfaces every operation as EPERM client-side without ever
        issuing CREATE/MKDIR/READDIR for child paths.
        """
        if dir_path == "":
            parent_path = ""
            parent_fileid = ROOT_FILEID
        else:
            parent_path = dir_path.rsplit("/", 1)[0] if "/" in dir_path else ""
            parent_attr = self._stat(parent_path)
            parent_fileid = parent_attr.fileid if parent_attr is not None else ROOT_FILEID
        entries: list[tuple[str, int, str]] = [
            (".", dir_attr.fileid, dir_path),
            ("..", parent_fileid, parent_path),
        ]
        for name in self._children(dir_path):
            child_path = _join(dir_path, name)
            entries.append((name, self.handles.fileid(child_path), child_path))
        return entries

    def proc_fsstat(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        attr = self._stat(path) if path is not None else None
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_post_op_attr(out, attr)
        out.pack_uint64(1 << 40)  # tbytes
        out.pack_uint64(1 << 40)  # fbytes
        out.pack_uint64(1 << 40)  # abytes
        out.pack_uint64(1 << 30)  # tfiles
        out.pack_uint64(1 << 30)  # ffiles
        out.pack_uint64(1 << 30)  # afiles
        out.pack_uint32(0)  # invarsec
        return out.bytes()

    def proc_fsinfo(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        attr = self._stat(path) if path is not None else None
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_post_op_attr(out, attr)
        out.pack_uint32(1 << 20)  # rtmax
        out.pack_uint32(1 << 20)  # rtpref
        out.pack_uint32(4096)  # rtmult
        out.pack_uint32(1 << 20)  # wtmax
        out.pack_uint32(1 << 20)  # wtpref
        out.pack_uint32(4096)  # wtmult
        out.pack_uint32(8192)  # dtpref
        out.pack_uint64(1 << 50)  # maxfilesize
        out.pack_uint32(1)  # time_delta seconds
        out.pack_uint32(0)  # time_delta nseconds
        # RFC 1813 §3.3.19 properties: HOMOGENEOUS=0x08, CANSETTIME=0x10.
        # No hardlinks (LINK=0x01) and no symlinks (SYMLINK=0x02) yet.
        out.pack_uint32(0x08 | 0x10)
        return out.bytes()

    def proc_pathconf(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        attr = self._stat(path) if path is not None else None
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_post_op_attr(out, attr)
        out.pack_uint32(255)  # linkmax
        out.pack_uint32(255)  # name_max
        out.pack_bool(True)  # no_trunc — server rejects names > name_max
        out.pack_bool(True)  # chown_restricted
        out.pack_bool(False)  # case_insensitive — we serve POSIX, names are case-sensitive
        out.pack_bool(True)  # case_preserving
        return out.bytes()

    def proc_commit(self, unp: Unpacker) -> bytes:
        path = self._resolve_handle(unp)
        if path is None:
            return _status(NFS3ERR_STALE)
        offset = unp.unpack_uint64()
        count = unp.unpack_uint32()
        size = self._file_size(path)
        if count == 0 or offset + count >= size:
            self._flush_path(path)
        before = self._stat(path)
        out = Packer()
        out.pack_uint32(NFS3_OK)
        _pack_wcc(out, before, before)
        out.pack_fopaque(WRITE_VERIFIER, 8)
        return out.bytes()

    # ---- Internal helpers ----

    def _resolve_handle(self, unp: Unpacker) -> str | None:
        handle = unp.unpack_opaque()
        return self.handles.resolve(handle)

    def _stat(self, path: str | None) -> _Attr | None:
        if path is None:
            return None
        if path == "":
            return _Attr(
                type=NF3DIR,
                mode=S_IFDIR | 0o755,
                nlink=2,
                size=DIR_SIZE,
                fileid=ROOT_FILEID,
                mtime=self._mtimes.get(path, self._epoch),
            )
        if path in self._ephemeral_files:
            return _Attr(
                type=NF3REG,
                mode=S_IFREG | 0o600,
                nlink=1,
                size=len(self._ephemeral_files[path]),
                fileid=self.handles.fileid(path),
                mtime=self._mtimes.get(path, self._epoch),
            )
        kind = self._classify(path)
        if kind is None:
            return None
        if kind == "dir":
            return _Attr(
                type=NF3DIR,
                mode=S_IFDIR | 0o755,
                nlink=2,
                size=DIR_SIZE,
                fileid=self.handles.fileid(path),
                mtime=self._mtimes.get(path, self._epoch),
            )
        try:
            size = self._file_size(path)
        except ObjectNotFound:
            return None
        return _Attr(
            type=NF3REG,
            mode=S_IFREG | 0o644,
            nlink=1,
            size=size,
            fileid=self.handles.fileid(path),
            mtime=self._mtimes.get(path, self._epoch),
        )

    def _classify(self, path: str) -> str | None:
        self._ensure_namespace()
        assert self._files_cache is not None
        assert self._dirs_cache is not None
        if path in self._files_cache:
            return "file"
        if path in self._dirs_cache:
            return "dir"
        return None

    def _children(self, dir_path: str) -> list[str]:
        self._ensure_namespace()
        assert self._children_cache is not None
        return sorted(c for c in self._children_cache.get(dir_path, set()) if c != ".trunkskeep")

    def _all_files_under(self, dir_path: str) -> list[str]:
        prefix = f"{dir_path}/"
        repo_files = [e.path for e in self._entries() if e.path.startswith(prefix)]
        metadata_files = [path for path in self._ephemeral_files if path.startswith(prefix)]
        return repo_files + metadata_files

    def _move_directory(self, source: str, dest: str) -> None:
        for path in self._all_files_under(source):
            new_path = dest + path[len(source) :]
            if path in self._ephemeral_files:
                self._ephemeral_files[new_path] = self._ephemeral_files.pop(path)
            else:
                self._flush_path(path)
                self.repo.move_file(path, new_path)
            self.handles.rename(path, new_path)
        self.handles.rename(source, dest)
        self._invalidate_namespace(source)
        self._invalidate_namespace(dest)

    def _entries(self) -> list[IndexEntry]:
        if self._entries_cache is None:
            self._entries_cache = self.repo.index_entries()
        return self._entries_cache

    def _ensure_namespace(self) -> None:
        if self._files_cache is not None and self._dirs_cache is not None and self._children_cache is not None:
            return
        entries = self._entries()
        files = {entry.path for entry in entries}
        dirs = {""}
        children: dict[str, set[str]] = {"": set()}
        for entry in entries:
            parent = ""
            parts = entry.path.split("/")
            for index, part in enumerate(parts):
                children.setdefault(parent, set()).add(part)
                if index < len(parts) - 1:
                    current = part if not parent else f"{parent}/{part}"
                    dirs.add(current)
                    children.setdefault(current, set())
                    parent = current
        self._files_cache = files
        self._dirs_cache = dirs
        self._children_cache = children

    def _file_size(self, path: str) -> int:
        buffered = self._write_buffers.get(path)
        if buffered is not None:
            return len(buffered)
        if path in self._ephemeral_files:
            return len(self._ephemeral_files[path])
        cached = self._size_cache.get(path)
        if cached is not None:
            return cached
        size = len(self.repo.read_file(path))
        self._size_cache[path] = size
        return size

    def _invalidate_namespace(self, path: str | None = None) -> None:
        self._entries_cache = None
        self._files_cache = None
        self._dirs_cache = None
        self._children_cache = None
        if path is None:
            self._size_cache.clear()
            return
        prefix = f"{path}/"
        for cached_path in list(self._size_cache):
            if cached_path == path or cached_path.startswith(prefix):
                self._size_cache.pop(cached_path, None)

    def _read_bytes(self, path: str) -> bytes:
        buffered = self._write_buffers.get(path)
        if buffered is not None:
            return bytes(buffered)
        if path in self._ephemeral_files:
            return self._ephemeral_files[path]
        return self.repo.read_file(path)

    def _write_bytes(self, path: str, data: bytes) -> None:
        self._write_buffers.pop(path, None)
        self._dirty_buffers.discard(path)
        self._dirty_generations.pop(path, None)
        self._stage_generations.pop(path, None)
        self._stage_futures.pop(path, None)
        self._cancel_stage_timer(path)
        if self._is_ephemeral_path(path):
            self._ephemeral_files[path] = data
            return
        self.repo.write_file(path, data)
        self._invalidate_namespace(path)

    def _delete_bytes(self, path: str) -> None:
        self._write_buffers.pop(path, None)
        self._dirty_buffers.discard(path)
        self._dirty_generations.pop(path, None)
        self._stage_generations.pop(path, None)
        self._stage_futures.pop(path, None)
        self._cancel_stage_timer(path)
        if path in self._ephemeral_files:
            self._ephemeral_files.pop(path, None)
            return
        self.repo.delete_file(path)
        self._invalidate_namespace(path)

    def _flush_path(self, path: str) -> None:
        if path not in self._dirty_buffers:
            return
        self._cancel_stage_timer(path)
        data = bytes(self._write_buffers[path])
        generation = self._dirty_generations[path]
        if self._is_ephemeral_path(path):
            self._ephemeral_files[path] = data
        else:
            staged = self._staged_blob(path, generation)
            if staged is None:
                self.repo.write_file(path, data)
            else:
                self.repo.set_file_object(path, staged.oid)
        self._write_buffers.pop(path, None)
        self._dirty_buffers.remove(path)
        self._dirty_generations.pop(path, None)
        self._stage_generations.pop(path, None)
        self._stage_futures.pop(path, None)
        self._size_cache[path] = len(data)
        self._invalidate_namespace(path)

    def _maybe_stage_background(self, path: str) -> None:
        if self._is_ephemeral_path(path):
            return
        current = self._stage_futures.get(path)
        if current is not None and not current.done():
            return
        buffer = self._write_buffers.get(path)
        if buffer is None:
            return
        size = len(buffer)
        if size < BACKGROUND_STAGE_MIN_BYTES:
            return
        self._cancel_stage_timer(path)
        if BACKGROUND_STAGE_DELAY_SECONDS <= 0:
            self._start_stage(path)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._stage_timers[path] = loop.call_later(
            BACKGROUND_STAGE_DELAY_SECONDS,
            self._start_stage,
            path,
        )

    def _cancel_stage_timer(self, path: str) -> None:
        timer = self._stage_timers.pop(path, None)
        if timer is not None:
            timer.cancel()

    def _start_stage(self, path: str) -> None:
        self._stage_timers.pop(path, None)
        current = self._stage_futures.get(path)
        if current is not None and not current.done():
            return
        buffer = self._write_buffers.get(path)
        generation = self._dirty_generations.get(path)
        if buffer is None or generation is None:
            return
        snapshot = bytes(buffer)
        self._stage_futures[path] = self._stage_executor.submit(self._stage_blob, generation, snapshot)
        self._stage_generations[path] = generation

    def _staged_blob(self, path: str, generation: int) -> _StagedBlob | None:
        future = self._stage_futures.get(path)
        if future is None:
            return None
        if not future.done():
            staged = future.result() if self._stage_generations.get(path) == generation else None
            return staged if staged is not None and staged.generation == generation else None
        try:
            staged = future.result()
        except Exception:
            return None
        return staged if staged.generation == generation else None

    def _stage_blob(self, generation: int, data: bytes) -> _StagedBlob:
        oid = self.repo.stage_blob(data)
        return _StagedBlob(generation=generation, oid=oid, size=len(data))

    def _is_ephemeral_path(self, path: str) -> bool:
        return any(part == ".DS_Store" or part.startswith("._") for part in path.split("/"))


# ---- Encoding helpers ----


def _status(stat: int) -> bytes:
    p = Packer()
    p.pack_uint32(stat)
    return p.bytes()


def _wcc_error(stat: int, before: _Attr | None) -> bytes:
    out = Packer()
    out.pack_uint32(stat)
    _pack_wcc(out, before, before)
    return out.bytes()


def _wcc_ok(before: _Attr | None, after: _Attr | None) -> bytes:
    out = Packer()
    out.pack_uint32(NFS3_OK)
    _pack_wcc(out, before, after)
    return out.bytes()


def _rename_wcc_error(stat: int, from_attr: _Attr | None, to_attr: _Attr | None) -> bytes:
    out = Packer()
    out.pack_uint32(stat)
    _pack_wcc(out, from_attr, from_attr)
    _pack_wcc(out, to_attr, to_attr)
    return out.bytes()


def _pack_handle(out: Packer, handle: bytes) -> None:
    out.pack_opaque(handle)


def _pack_post_op_handle(out: Packer, handle: bytes | None) -> None:
    if handle is None:
        out.pack_bool(False)
        return
    out.pack_bool(True)
    out.pack_opaque(handle)


def _pack_post_op_attr(out: Packer, attr: _Attr | None) -> None:
    if attr is None:
        out.pack_bool(False)
        return
    out.pack_bool(True)
    _pack_fattr(out, attr)


def _pack_fattr(out: Packer, attr: _Attr) -> None:
    out.pack_uint32(attr.type)
    out.pack_uint32(attr.mode)
    out.pack_uint32(attr.nlink)
    out.pack_uint32(_OWNER_UID)
    out.pack_uint32(_OWNER_GID)
    out.pack_uint64(attr.size)
    out.pack_uint64(attr.size)  # used == size
    out.pack_uint32(0)  # rdev.specdata1
    out.pack_uint32(0)  # rdev.specdata2
    out.pack_uint64(FSID)
    out.pack_uint64(attr.fileid)
    seconds = int(attr.mtime)
    nsec = int((attr.mtime - seconds) * 1_000_000_000)
    out.pack_uint32(seconds)
    out.pack_uint32(nsec)
    out.pack_uint32(seconds)
    out.pack_uint32(nsec)
    out.pack_uint32(seconds)
    out.pack_uint32(nsec)


def _pack_wcc(out: Packer, before: _Attr | None, after: _Attr | None) -> None:
    # pre_op_attr: bool + (size, mtime, ctime)
    if before is None:
        out.pack_bool(False)
    else:
        out.pack_bool(True)
        out.pack_uint64(before.size)
        seconds = int(before.mtime)
        nsec = int((before.mtime - seconds) * 1_000_000_000)
        out.pack_uint32(seconds)
        out.pack_uint32(nsec)
        out.pack_uint32(seconds)
        out.pack_uint32(nsec)
    _pack_post_op_attr(out, after)


def _read_set_uint32(unp: Unpacker) -> int | None:
    if unp.unpack_bool():
        return unp.unpack_uint32()
    return None


def _read_set_time(unp: Unpacker) -> tuple[int, int] | None:
    flag = unp.unpack_uint32()
    if flag == DONT_CHANGE:
        return None
    if flag == SET_TO_SERVER_TIME:
        return None
    if flag == SET_TO_CLIENT_TIME:
        return _read_nfstime(unp)
    return None


def _read_nfstime(unp: Unpacker) -> tuple[int, int]:
    return unp.unpack_uint32(), unp.unpack_uint32()


def _read_sattr(unp: Unpacker) -> None:
    _read_set_uint32(unp)  # mode
    _read_set_uint32(unp)  # uid
    _read_set_uint32(unp)  # gid
    if unp.unpack_bool():  # size set?
        unp.unpack_uint64()
    _read_set_time(unp)
    _read_set_time(unp)


def _name_ok(name: str) -> bool:
    if not name or len(name.encode("utf-8")) > 255:
        return False
    if name in {".", ".."} or "/" in name or "\\" in name:
        return False
    return not any(ord(c) < 0x20 or c == "\x7f" for c in name)


def _join(dir_path: str, name: str) -> str:
    return name if not dir_path else f"{dir_path}/{name}"


_PROCS: dict[int, Callable[["NfsHandler", Unpacker], bytes]] = {
    NFSPROC3_NULL: NfsHandler.proc_null,
    NFSPROC3_GETATTR: NfsHandler.proc_getattr,
    NFSPROC3_SETATTR: NfsHandler.proc_setattr,
    NFSPROC3_LOOKUP: NfsHandler.proc_lookup,
    NFSPROC3_ACCESS: NfsHandler.proc_access,
    NFSPROC3_READLINK: NfsHandler.proc_readlink,
    NFSPROC3_READ: NfsHandler.proc_read,
    NFSPROC3_WRITE: NfsHandler.proc_write,
    NFSPROC3_CREATE: NfsHandler.proc_create,
    NFSPROC3_MKDIR: NfsHandler.proc_mkdir,
    NFSPROC3_SYMLINK: NfsHandler.proc_symlink,
    NFSPROC3_MKNOD: NfsHandler.proc_mknod,
    NFSPROC3_REMOVE: NfsHandler.proc_remove,
    NFSPROC3_RMDIR: NfsHandler.proc_rmdir,
    NFSPROC3_RENAME: NfsHandler.proc_rename,
    NFSPROC3_LINK: NfsHandler.proc_link,
    NFSPROC3_READDIR: NfsHandler.proc_readdir,
    NFSPROC3_READDIRPLUS: NfsHandler.proc_readdirplus,
    NFSPROC3_FSSTAT: NfsHandler.proc_fsstat,
    NFSPROC3_FSINFO: NfsHandler.proc_fsinfo,
    NFSPROC3_PATHCONF: NfsHandler.proc_pathconf,
    NFSPROC3_COMMIT: NfsHandler.proc_commit,
}
