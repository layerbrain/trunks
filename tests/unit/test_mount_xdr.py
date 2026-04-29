from __future__ import annotations

import unittest

from trunks.mount.xdr import Packer, Unpacker, XdrError


class XdrPrimitiveTests(unittest.TestCase):
    def test_uint32_round_trip(self) -> None:
        for value in (0, 1, 0x7FFFFFFF, 0xFFFFFFFF):
            packer = Packer()
            packer.pack_uint32(value)
            self.assertEqual(len(packer), 4)
            self.assertEqual(Unpacker(packer.bytes()).unpack_uint32(), value)

    def test_uint32_rejects_out_of_range(self) -> None:
        packer = Packer()
        with self.assertRaises(XdrError):
            packer.pack_uint32(-1)
        with self.assertRaises(XdrError):
            packer.pack_uint32(0x1_0000_0000)

    def test_int32_round_trip(self) -> None:
        for value in (-0x80000000, -1, 0, 1, 0x7FFFFFFF):
            packer = Packer()
            packer.pack_int32(value)
            self.assertEqual(Unpacker(packer.bytes()).unpack_int32(), value)

    def test_uint64_round_trip(self) -> None:
        for value in (0, 1, 0xFFFFFFFFFFFFFFFF):
            packer = Packer()
            packer.pack_uint64(value)
            self.assertEqual(len(packer), 8)
            self.assertEqual(Unpacker(packer.bytes()).unpack_uint64(), value)

    def test_bool_round_trip(self) -> None:
        for value in (True, False):
            packer = Packer()
            packer.pack_bool(value)
            self.assertEqual(Unpacker(packer.bytes()).unpack_bool(), value)

    def test_opaque_pads_to_four_bytes(self) -> None:
        packer = Packer()
        packer.pack_opaque(b"abc")
        self.assertEqual(packer.bytes(), b"\x00\x00\x00\x03abc\x00")
        self.assertEqual(Unpacker(packer.bytes()).unpack_opaque(), b"abc")

    def test_opaque_no_padding_when_aligned(self) -> None:
        packer = Packer()
        packer.pack_opaque(b"abcd")
        self.assertEqual(packer.bytes(), b"\x00\x00\x00\x04abcd")
        self.assertEqual(Unpacker(packer.bytes()).unpack_opaque(), b"abcd")

    def test_string_round_trip(self) -> None:
        packer = Packer()
        packer.pack_string("héllo")
        self.assertEqual(Unpacker(packer.bytes()).unpack_string(), "héllo")

    def test_fixed_opaque_round_trip(self) -> None:
        packer = Packer()
        packer.pack_fopaque(b"\x01\x02\x03\x04\x05", 5)
        # 5 bytes + 3 pad = 8 bytes total
        self.assertEqual(len(packer), 8)
        unpacker = Unpacker(packer.bytes())
        self.assertEqual(unpacker.unpack_fopaque(5), b"\x01\x02\x03\x04\x05")
        self.assertTrue(unpacker.done())

    def test_truncated_stream_raises(self) -> None:
        unpacker = Unpacker(b"\x00\x00\x00")  # 3 bytes, need 4
        with self.assertRaises(XdrError):
            unpacker.unpack_uint32()

    def test_compound_round_trip(self) -> None:
        packer = Packer()
        packer.pack_uint32(7)
        packer.pack_string("hi")
        packer.pack_uint64(123456789012345)
        packer.pack_bool(True)
        packer.pack_opaque(b"")

        unpacker = Unpacker(packer.bytes())
        self.assertEqual(unpacker.unpack_uint32(), 7)
        self.assertEqual(unpacker.unpack_string(), "hi")
        self.assertEqual(unpacker.unpack_uint64(), 123456789012345)
        self.assertEqual(unpacker.unpack_bool(), True)
        self.assertEqual(unpacker.unpack_opaque(), b"")
        self.assertTrue(unpacker.done())


if __name__ == "__main__":
    unittest.main()
