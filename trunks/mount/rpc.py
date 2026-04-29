"""ONC RPC v2 framing (RFC 5531).

This module handles the wire format for ONC RPC v2 over TCP: the record-marking
fragment layer plus the call/reply message structure. Authentication is
accepted as AUTH_NONE or AUTH_SYS without verification — the daemon only binds
to 127.0.0.1 and trusts the local kernel client.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from .xdr import Packer, Unpacker

LAST_FRAG_BIT = 0x80000000
MAX_RECORD = 16 * 1024 * 1024
MAX_FRAGMENT = MAX_RECORD


# Message type
MSG_CALL = 0
MSG_REPLY = 1

# Reply status
MSG_ACCEPTED = 0
MSG_DENIED = 1

# Accept status
SUCCESS = 0
PROG_UNAVAIL = 1
PROG_MISMATCH = 2
PROC_UNAVAIL = 3
GARBAGE_ARGS = 4
SYSTEM_ERR = 5

# Reject status
RPC_MISMATCH = 0
AUTH_ERROR = 1

# Auth flavors
AUTH_NONE = 0
AUTH_SYS = 1


class RpcError(ValueError):
    """Raised when the RPC layer cannot make sense of incoming bytes."""


@dataclass(frozen=True)
class RpcCall:
    xid: int
    rpcvers: int
    prog: int
    vers: int
    proc: int
    cred_flavor: int
    cred_body: bytes
    verf_flavor: int
    verf_body: bytes
    args: bytes


async def read_record(reader: asyncio.StreamReader) -> bytes:
    """Read one full RPC record (concatenated fragments)."""
    payload = bytearray()
    while True:
        header = await _read_exact(reader, 4)
        marker = struct.unpack(">I", header)[0]
        last = bool(marker & LAST_FRAG_BIT)
        size = marker & ~LAST_FRAG_BIT
        if size > MAX_FRAGMENT:
            raise RpcError(f"fragment too large: {size}")
        if size:
            payload.extend(await _read_exact(reader, size))
            if len(payload) > MAX_RECORD:
                raise RpcError(f"record too large: {len(payload)}")
        if last:
            return bytes(payload)


def encode_record(payload: bytes) -> bytes:
    """Wrap a payload as a single-fragment RPC record."""
    if len(payload) > MAX_FRAGMENT:
        raise RpcError(f"reply too large: {len(payload)}")
    return struct.pack(">I", LAST_FRAG_BIT | len(payload)) + payload


def parse_call(record: bytes) -> RpcCall:
    """Parse a CALL message; raises RpcError on a non-CALL or malformed record."""
    unp = Unpacker(record)
    xid = unp.unpack_uint32()
    mtype = unp.unpack_uint32()
    if mtype != MSG_CALL:
        raise RpcError(f"expected CALL, got mtype {mtype}")
    rpcvers = unp.unpack_uint32()
    prog = unp.unpack_uint32()
    vers = unp.unpack_uint32()
    proc = unp.unpack_uint32()
    cred_flavor = unp.unpack_uint32()
    cred_body = unp.unpack_opaque()
    verf_flavor = unp.unpack_uint32()
    verf_body = unp.unpack_opaque()
    args = record[unp.offset :]
    return RpcCall(
        xid=xid,
        rpcvers=rpcvers,
        prog=prog,
        vers=vers,
        proc=proc,
        cred_flavor=cred_flavor,
        cred_body=cred_body,
        verf_flavor=verf_flavor,
        verf_body=verf_body,
        args=args,
    )


def pack_reply_success(xid: int, body: bytes) -> bytes:
    """Encode a MSG_ACCEPTED / SUCCESS reply with the given proc result body."""
    p = Packer()
    p.pack_uint32(xid)
    p.pack_uint32(MSG_REPLY)
    p.pack_uint32(MSG_ACCEPTED)
    p.pack_uint32(AUTH_NONE)
    p.pack_opaque(b"")
    p.pack_uint32(SUCCESS)
    return p.bytes() + body


def pack_reply_accepted_error(xid: int, accept_stat: int) -> bytes:
    """Encode a MSG_ACCEPTED reply with PROG_UNAVAIL/PROC_UNAVAIL/etc."""
    if accept_stat == PROG_MISMATCH:
        raise RpcError("PROG_MISMATCH requires version range; use pack_reply_prog_mismatch")
    p = Packer()
    p.pack_uint32(xid)
    p.pack_uint32(MSG_REPLY)
    p.pack_uint32(MSG_ACCEPTED)
    p.pack_uint32(AUTH_NONE)
    p.pack_opaque(b"")
    p.pack_uint32(accept_stat)
    return p.bytes()


def pack_reply_prog_mismatch(xid: int, low: int, high: int) -> bytes:
    p = Packer()
    p.pack_uint32(xid)
    p.pack_uint32(MSG_REPLY)
    p.pack_uint32(MSG_ACCEPTED)
    p.pack_uint32(AUTH_NONE)
    p.pack_opaque(b"")
    p.pack_uint32(PROG_MISMATCH)
    p.pack_uint32(low)
    p.pack_uint32(high)
    return p.bytes()


def pack_reply_rpc_mismatch(xid: int, low: int, high: int) -> bytes:
    p = Packer()
    p.pack_uint32(xid)
    p.pack_uint32(MSG_REPLY)
    p.pack_uint32(MSG_DENIED)
    p.pack_uint32(RPC_MISMATCH)
    p.pack_uint32(low)
    p.pack_uint32(high)
    return p.bytes()


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    """Read exactly n bytes; raise RpcError on EOF."""
    data = await reader.readexactly(n)
    if len(data) != n:
        raise RpcError(f"short read: expected {n}, got {len(data)}")
    return data
