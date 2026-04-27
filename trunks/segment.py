from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from .errors import ObjectNotFound
from .ids import ObjectId
from .objects import parse_canonical_object

MAGIC = b"TRUNKSSEG1\0"
VERSION = 1


@dataclass(frozen=True)
class SegmentEntry:
    oid: ObjectId
    kind: str
    offset: int
    length: int


@dataclass(frozen=True)
class SegmentIndex:
    data_sha256: str
    entries: tuple[SegmentEntry, ...]


def encode_segment(objects: Iterable[tuple[ObjectId, bytes]]) -> tuple[str, bytes, bytes]:
    unique: dict[ObjectId, bytes] = {}
    for oid, data in objects:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        unique.setdefault(oid, data)

    payload = bytearray(MAGIC)
    entries: list[SegmentEntry] = []
    for oid in sorted(unique):
        data = unique[oid]
        kind, _ = parse_canonical_object(data)
        offset = len(payload)
        payload.extend(data)
        entries.append(SegmentEntry(oid=oid, kind=kind, offset=offset, length=len(data)))

    segment = bytes(payload)
    doc = {
        "version": VERSION,
        "data_sha256": hashlib.sha256(segment).hexdigest(),
        "objects": [
            {"oid": str(entry.oid), "kind": entry.kind, "offset": entry.offset, "length": entry.length}
            for entry in entries
        ],
    }
    index = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
    segment_id = hashlib.sha256(index + b"\0" + segment).hexdigest()
    return segment_id, segment, index


def decode_index(data: bytes) -> SegmentIndex:
    doc = json.loads(data.decode())
    if doc.get("version") != VERSION:
        raise ValueError("unsupported segment index version")
    entries = tuple(
        SegmentEntry(
            oid=ObjectId(item["oid"]),
            kind=item["kind"],
            offset=int(item["offset"]),
            length=int(item["length"]),
        )
        for item in doc.get("objects", [])
    )
    return SegmentIndex(data_sha256=doc["data_sha256"], entries=entries)


def read_segment_object(segment: bytes, index: bytes, oid: ObjectId) -> bytes:
    decoded = decode_index(index)
    if not segment.startswith(MAGIC):
        raise ValueError("invalid segment magic")
    if hashlib.sha256(segment).hexdigest() != decoded.data_sha256:
        raise ValueError("segment checksum mismatch")

    for entry in decoded.entries:
        if entry.oid != oid:
            continue
        end = entry.offset + entry.length
        if entry.offset < len(MAGIC) or end > len(segment):
            raise ValueError("segment entry points outside data")
        data = segment[entry.offset:end]
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("segment object checksum mismatch")
        kind, _ = parse_canonical_object(data)
        if kind != entry.kind:
            raise ValueError("segment object kind mismatch")
        return data
    raise ObjectNotFound(str(oid))
