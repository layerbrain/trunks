from __future__ import annotations

import asyncio
import io
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trunks.cli import dispatch
from trunks.mount import nfs, mountd
from trunks.mount.rpc import (
    AUTH_NONE,
    LAST_FRAG_BIT,
    MSG_ACCEPTED,
    MSG_CALL,
    MSG_REPLY,
    SUCCESS,
    encode_record,
)
from trunks.mount.server import NfsServer
from trunks.mount.xdr import Packer, Unpacker
from trunks.repository import Repository


def _build_call(*, xid: int, prog: int, proc: int, args: bytes = b"") -> bytes:
    p = Packer()
    p.pack_uint32(xid)
    p.pack_uint32(MSG_CALL)
    p.pack_uint32(2)  # rpcvers
    p.pack_uint32(prog)
    p.pack_uint32(3)  # vers
    p.pack_uint32(proc)
    p.pack_uint32(AUTH_NONE)
    p.pack_opaque(b"")
    p.pack_uint32(AUTH_NONE)
    p.pack_opaque(b"")
    return p.bytes() + args


async def _send_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, call: bytes) -> bytes:
    writer.write(encode_record(call))
    await writer.drain()
    header = await reader.readexactly(4)
    marker = struct.unpack(">I", header)[0]
    size = marker & ~LAST_FRAG_BIT
    return await reader.readexactly(size)


class NfsServerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path.cwd()
        os.chdir(self.tmp.name)
        with redirect_stdout(io.StringIO()):
            await dispatch(["init", "--name", "demo"])
        self.repo = Repository.find(Path(self.tmp.name))
        self.server = NfsServer(self.repo, export_name="demo", port=0)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    async def test_null_call_round_trips_over_tcp(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        try:
            call = _build_call(xid=42, prog=nfs.PROGRAM, proc=nfs.NFSPROC3_NULL)
            payload = await _send_call(reader, writer, call)
        finally:
            writer.close()
            await writer.wait_closed()

        unp = Unpacker(payload)
        self.assertEqual(unp.unpack_uint32(), 42)  # xid
        self.assertEqual(unp.unpack_uint32(), MSG_REPLY)
        self.assertEqual(unp.unpack_uint32(), MSG_ACCEPTED)
        unp.unpack_uint32()  # verf flavor
        unp.unpack_opaque()  # verf body
        self.assertEqual(unp.unpack_uint32(), SUCCESS)

    async def test_mount_call_returns_root_handle(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        try:
            args = Packer()
            args.pack_string("/demo")
            call = _build_call(
                xid=7,
                prog=mountd.PROGRAM,
                proc=mountd.MOUNTPROC3_MNT,
                args=args.bytes(),
            )
            payload = await _send_call(reader, writer, call)
        finally:
            writer.close()
            await writer.wait_closed()

        unp = Unpacker(payload)
        unp.unpack_uint32()  # xid
        unp.unpack_uint32()  # mtype
        unp.unpack_uint32()  # accepted
        unp.unpack_uint32()  # verf flavor
        unp.unpack_opaque()  # verf body
        self.assertEqual(unp.unpack_uint32(), SUCCESS)
        self.assertEqual(unp.unpack_uint32(), mountd.MNT3_OK)
        self.assertEqual(unp.unpack_opaque(), self.server.handles.root())


if __name__ == "__main__":
    unittest.main()
