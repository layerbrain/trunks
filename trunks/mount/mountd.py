"""Mount protocol v3 (RFC 1813 Appendix I).

Two procedures matter for a working mount: MNT (resolve an export path to a
file handle) and UMNT (drop the client). The OS NFS client calls MNT first to
obtain the root handle, then NFS GETATTR / FSINFO / etc.

We expose a single export per repo at "/<repo-name>".
"""

from __future__ import annotations

from typing import Callable

from .handles import HandleTable
from .rpc import RpcCall, pack_reply_accepted_error, pack_reply_success, GARBAGE_ARGS, PROC_UNAVAIL
from .xdr import Packer, Unpacker, XdrError

PROGRAM = 100005
VERSION = 3

# Procedures
MOUNTPROC3_NULL = 0
MOUNTPROC3_MNT = 1
MOUNTPROC3_DUMP = 2
MOUNTPROC3_UMNT = 3
MOUNTPROC3_UMNTALL = 4
MOUNTPROC3_EXPORT = 5

# Mount status (mountstat3)
MNT3_OK = 0
MNT3ERR_PERM = 1
MNT3ERR_NOENT = 2
MNT3ERR_IO = 5
MNT3ERR_ACCES = 13
MNT3ERR_NOTDIR = 20
MNT3ERR_INVAL = 22
MNT3ERR_NAMETOOLONG = 63
MNT3ERR_NOTSUPP = 10004
MNT3ERR_SERVERFAULT = 10006

# RFC 1813 lists AUTH_NONE, AUTH_SYS as the standard flavors a server advertises.
_AUTH_FLAVORS = (0, 1)


class MountHandler:
    """Dispatches Mount-protocol v3 procedures."""

    def __init__(self, export_name: str, handles: HandleTable) -> None:
        # Export path the client passes to MNT — e.g. "/demo".
        self.export = "/" + export_name.strip("/")
        self.handles = handles
        self._mnt_enabled = True

    def disable_mnt(self) -> None:
        self._mnt_enabled = False

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
            return pack_reply_success(call.xid, _status(MNT3ERR_SERVERFAULT))

    def proc_null(self, _: Unpacker) -> bytes:
        return b""

    def proc_mnt(self, unp: Unpacker) -> bytes:
        path = unp.unpack_string()
        if not self._mnt_enabled:
            return _status(MNT3ERR_ACCES)
        if path.rstrip("/") != self.export.rstrip("/"):
            return _status(MNT3ERR_NOENT)
        out = Packer()
        out.pack_uint32(MNT3_OK)
        out.pack_opaque(self.handles.root())
        out.pack_uint32(len(_AUTH_FLAVORS))
        for flavor in _AUTH_FLAVORS:
            out.pack_uint32(flavor)
        return out.bytes()

    def proc_dump(self, _: Unpacker) -> bytes:
        # Empty mountlist: no value flag, then end of list.
        out = Packer()
        out.pack_bool(False)
        return out.bytes()

    def proc_umnt(self, unp: Unpacker) -> bytes:
        unp.unpack_string()  # accepted, ignored
        return b""

    def proc_umntall(self, _: Unpacker) -> bytes:
        return b""

    def proc_export(self, _: Unpacker) -> bytes:
        out = Packer()
        # exportnode list: [(dirpath, [groups])]
        out.pack_bool(True)
        out.pack_string(self.export)
        out.pack_bool(False)  # empty groups list
        out.pack_bool(False)  # end of exports
        return out.bytes()


def _status(stat: int) -> bytes:
    p = Packer()
    p.pack_uint32(stat)
    return p.bytes()


_PROCS: dict[int, Callable[["MountHandler", Unpacker], bytes]] = {
    MOUNTPROC3_NULL: MountHandler.proc_null,
    MOUNTPROC3_MNT: MountHandler.proc_mnt,
    MOUNTPROC3_DUMP: MountHandler.proc_dump,
    MOUNTPROC3_UMNT: MountHandler.proc_umnt,
    MOUNTPROC3_UMNTALL: MountHandler.proc_umntall,
    MOUNTPROC3_EXPORT: MountHandler.proc_export,
}
