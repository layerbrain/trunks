from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ObjectId:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 40 or any(c not in "0123456789abcdef" for c in self.value):
            raise ValueError(f"invalid SHA-1 object id: {self.value!r}")

    @classmethod
    def from_bytes(cls, data: bytes) -> "ObjectId":
        return cls(hashlib.sha1(data).hexdigest())

    @classmethod
    def parse(cls, value: str | "ObjectId") -> "ObjectId":
        if isinstance(value, ObjectId):
            return value
        return cls(value)

    def raw(self) -> bytes:
        return bytes.fromhex(self.value)

    def short(self, length: int = 12) -> str:
        return self.value[:length]

    def __str__(self) -> str:
        return self.value


Sha1 = ObjectId


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    value = (timestamp_ms << 80) | randomness
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


TxId = str

