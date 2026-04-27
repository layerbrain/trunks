from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .ids import ObjectId

CHUNK_SIZE = 4 * 1024 * 1024
CHUNK_THRESHOLD = CHUNK_SIZE
VERSION = 1


@dataclass(frozen=True)
class ChunkEntry:
    id: str
    offset: int
    length: int


@dataclass(frozen=True)
class ChunkManifest:
    oid: ObjectId
    size: int
    chunks: tuple[ChunkEntry, ...]


def should_chunk(data: bytes) -> bool:
    return len(data) > CHUNK_THRESHOLD


def encode_chunked_object(oid: ObjectId, data: bytes, *, chunk_size: int = CHUNK_SIZE) -> tuple[bytes, tuple[tuple[str, bytes], ...]]:
    if ObjectId.from_bytes(data) != oid:
        raise ValueError("object id does not match object data")
    chunks: list[tuple[str, bytes]] = []
    entries: list[ChunkEntry] = []
    for offset in range(0, len(data), chunk_size):
        chunk = data[offset : offset + chunk_size]
        chunk_id = hashlib.sha256(chunk).hexdigest()
        chunks.append((chunk_id, chunk))
        entries.append(ChunkEntry(id=chunk_id, offset=offset, length=len(chunk)))
    manifest = {
        "version": VERSION,
        "oid": str(oid),
        "size": len(data),
        "chunks": [
            {"id": entry.id, "offset": entry.offset, "length": entry.length}
            for entry in entries
        ],
    }
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode(), tuple(chunks)


def decode_manifest(data: bytes) -> ChunkManifest:
    raw = json.loads(data.decode())
    if raw.get("version") != VERSION:
        raise ValueError("unsupported chunk manifest version")
    return ChunkManifest(
        oid=ObjectId(raw["oid"]),
        size=int(raw["size"]),
        chunks=tuple(
            ChunkEntry(id=item["id"], offset=int(item["offset"]), length=int(item["length"]))
            for item in raw.get("chunks", [])
        ),
    )


def assemble_chunked_object(manifest_bytes: bytes, chunks: list[bytes]) -> bytes:
    manifest = decode_manifest(manifest_bytes)
    if len(chunks) != len(manifest.chunks):
        raise ValueError("chunk count mismatch")
    out = bytearray(manifest.size)
    for entry, chunk in zip(manifest.chunks, chunks, strict=True):
        if len(chunk) != entry.length:
            raise ValueError("chunk length mismatch")
        if hashlib.sha256(chunk).hexdigest() != entry.id:
            raise ValueError("chunk checksum mismatch")
        out[entry.offset : entry.offset + entry.length] = chunk
    data = bytes(out)
    if ObjectId.from_bytes(data) != manifest.oid:
        raise ValueError("chunked object checksum mismatch")
    return data
