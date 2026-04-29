from __future__ import annotations

import asyncio
import struct
import unittest

from trunks.mount import rpc
from trunks.mount.xdr import Packer


def _build_call(
    *,
    xid: int = 0xDEADBEEF,
    prog: int = 100003,
    vers: int = 3,
    proc: int = 0,
    args: bytes = b"",
) -> bytes:
    p = Packer()
    p.pack_uint32(xid)
    p.pack_uint32(rpc.MSG_CALL)
    p.pack_uint32(2)  # rpcvers
    p.pack_uint32(prog)
    p.pack_uint32(vers)
    p.pack_uint32(proc)
    p.pack_uint32(rpc.AUTH_NONE)
    p.pack_opaque(b"")
    p.pack_uint32(rpc.AUTH_NONE)
    p.pack_opaque(b"")
    return p.bytes() + args


class RecordMarkingTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_record_single_fragment(self) -> None:
        payload = b"hello-record"
        framed = rpc.encode_record(payload)
        reader = asyncio.StreamReader()
        reader.feed_data(framed)
        reader.feed_eof()
        self.assertEqual(await rpc.read_record(reader), payload)

    async def test_read_record_multiple_fragments(self) -> None:
        first = b"part-one"
        second = b"part-two"
        # First fragment: not-last
        frame_a = struct.pack(">I", len(first)) + first
        frame_b = struct.pack(">I", rpc.LAST_FRAG_BIT | len(second)) + second
        reader = asyncio.StreamReader()
        reader.feed_data(frame_a + frame_b)
        reader.feed_eof()
        self.assertEqual(await rpc.read_record(reader), first + second)

    async def test_read_record_rejects_oversize(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", rpc.MAX_FRAGMENT + 1))
        reader.feed_eof()
        with self.assertRaises(rpc.RpcError):
            await rpc.read_record(reader)

    async def test_read_record_rejects_oversize_multi_fragment_record(self) -> None:
        first = b"a" * rpc.MAX_FRAGMENT
        second = b"b"
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", len(first)) + first)
        reader.feed_data(struct.pack(">I", rpc.LAST_FRAG_BIT | len(second)) + second)
        reader.feed_eof()
        with self.assertRaises(rpc.RpcError):
            await rpc.read_record(reader)

    def test_encode_record_sets_last_fragment_bit(self) -> None:
        framed = rpc.encode_record(b"x" * 5)
        marker = struct.unpack(">I", framed[:4])[0]
        self.assertTrue(marker & rpc.LAST_FRAG_BIT)
        self.assertEqual(marker & ~rpc.LAST_FRAG_BIT, 5)


class CallParsingTests(unittest.TestCase):
    def test_parse_minimal_call(self) -> None:
        record = _build_call(prog=100005, vers=3, proc=1)
        call = rpc.parse_call(record)
        self.assertEqual(call.xid, 0xDEADBEEF)
        self.assertEqual(call.rpcvers, 2)
        self.assertEqual(call.prog, 100005)
        self.assertEqual(call.vers, 3)
        self.assertEqual(call.proc, 1)
        self.assertEqual(call.cred_flavor, rpc.AUTH_NONE)
        self.assertEqual(call.verf_flavor, rpc.AUTH_NONE)
        self.assertEqual(call.args, b"")

    def test_parse_call_carries_proc_args(self) -> None:
        record = _build_call(args=b"\x00\x00\x00\x07opaque!\x00")
        call = rpc.parse_call(record)
        self.assertEqual(call.args, b"\x00\x00\x00\x07opaque!\x00")

    def test_parse_call_rejects_non_call(self) -> None:
        p = Packer()
        p.pack_uint32(0)
        p.pack_uint32(rpc.MSG_REPLY)
        with self.assertRaises(rpc.RpcError):
            rpc.parse_call(p.bytes())


class ReplyEncodingTests(unittest.TestCase):
    def test_pack_reply_success_layout(self) -> None:
        body = b"\x00\x00\x00\x42"
        framed = rpc.pack_reply_success(0xCAFEBABE, body)
        self.assertEqual(framed[:4], struct.pack(">I", 0xCAFEBABE))
        self.assertEqual(framed[4:8], struct.pack(">I", rpc.MSG_REPLY))
        self.assertEqual(framed[8:12], struct.pack(">I", rpc.MSG_ACCEPTED))
        # AUTH_NONE verifier (flavor + zero-length opaque) -> 8 bytes
        self.assertEqual(framed[12:20], b"\x00\x00\x00\x00\x00\x00\x00\x00")
        self.assertEqual(framed[20:24], struct.pack(">I", rpc.SUCCESS))
        self.assertEqual(framed[24:], body)

    def test_pack_reply_proc_unavail(self) -> None:
        framed = rpc.pack_reply_accepted_error(1, rpc.PROC_UNAVAIL)
        self.assertEqual(framed[20:24], struct.pack(">I", rpc.PROC_UNAVAIL))


if __name__ == "__main__":
    unittest.main()
