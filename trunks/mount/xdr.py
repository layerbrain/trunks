"""XDR encoding primitives (RFC 4506)."""

from __future__ import annotations

import struct


class XdrError(ValueError):
    pass


def _padding(length: int) -> int:
    return (-length) % 4


class Packer:
    def __init__(self) -> None:
        self._buf = bytearray()

    def __len__(self) -> int:
        return len(self._buf)

    def bytes(self) -> bytes:
        return bytes(self._buf)

    def pack_uint32(self, value: int) -> None:
        if value < 0 or value > 0xFFFF_FFFF:
            raise XdrError(f"uint32 out of range: {value}")
        self._buf.extend(struct.pack(">I", value))

    def pack_int32(self, value: int) -> None:
        if value < -0x8000_0000 or value > 0x7FFF_FFFF:
            raise XdrError(f"int32 out of range: {value}")
        self._buf.extend(struct.pack(">i", value))

    def pack_uint64(self, value: int) -> None:
        if value < 0 or value > 0xFFFF_FFFF_FFFF_FFFF:
            raise XdrError(f"uint64 out of range: {value}")
        self._buf.extend(struct.pack(">Q", value))

    def pack_bool(self, value: bool) -> None:
        self.pack_uint32(1 if value else 0)

    def pack_opaque(self, value: bytes) -> None:
        self.pack_uint32(len(value))
        self.pack_fopaque(value, len(value))

    def pack_fopaque(self, value: bytes, length: int) -> None:
        if len(value) != length:
            raise XdrError(f"fixed opaque length mismatch: expected {length}, got {len(value)}")
        self._buf.extend(value)
        self._buf.extend(b"\x00" * _padding(length))

    def pack_string(self, value: str) -> None:
        self.pack_opaque(value.encode("utf-8"))


class Unpacker:
    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self.offset = 0

    def done(self) -> bool:
        return self.offset == len(self._data)

    def _read(self, length: int) -> bytes:
        end = self.offset + length
        if end > len(self._data):
            raise XdrError("truncated XDR data")
        out = self._data[self.offset:end].tobytes()
        self.offset = end
        return out

    def unpack_uint32(self) -> int:
        return struct.unpack(">I", self._read(4))[0]

    def unpack_int32(self) -> int:
        return struct.unpack(">i", self._read(4))[0]

    def unpack_uint64(self) -> int:
        return struct.unpack(">Q", self._read(8))[0]

    def unpack_bool(self) -> bool:
        value = self.unpack_uint32()
        if value not in (0, 1):
            raise XdrError(f"invalid XDR bool: {value}")
        return value == 1

    def unpack_opaque(self) -> bytes:
        return self.unpack_fopaque(self.unpack_uint32())

    def unpack_fopaque(self, length: int) -> bytes:
        value = self._read(length)
        pad = _padding(length)
        if pad:
            self._read(pad)
        return value

    def unpack_string(self) -> str:
        return self.unpack_opaque().decode("utf-8")

__all__ = ["Packer", "Unpacker", "XdrError"]
