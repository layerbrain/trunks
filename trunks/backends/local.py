from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import AsyncIterator

from ..backend import Backend, Capabilities, Role
from ..chunked import assemble_chunked_object, decode_manifest, encode_chunked_object, should_chunk
from ..errors import BackendUnavailable, ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..segment import encode_segment, read_segment_object
from ..url import BackendURL

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class Local(Backend):
    def __init__(self, root: str | Path, *, role: Role = Role.primary) -> None:
        self.root = Path(root)
        self.url = BackendURL.parse(self.root.as_posix())
        self.role = role

    async def capabilities(self) -> Capabilities:
        return Capabilities(cas_refs=True, locks=False, journal=True, list_prefix=True, read_after_write_refs=True)

    async def read_object(self, oid: ObjectId) -> bytes:
        path = self._object_path(oid)
        if path.exists():
            return await asyncio.to_thread(path.read_bytes)
        try:
            return await asyncio.to_thread(self._read_chunked_object_sync, oid)
        except ObjectNotFound:
            pass
        return await asyncio.to_thread(self._read_segment_object_sync, oid)

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        if should_chunk(data):
            await asyncio.to_thread(self._write_chunked_object_sync, oid, data)
            return
        path = self._object_path(oid)
        if path.exists():
            existing = await asyncio.to_thread(path.read_bytes)
            if existing == data and ObjectId.from_bytes(existing) == oid:
                return
        await asyncio.to_thread(self._write_atomic, path, data)

    async def write_objects(self, objects: Iterable[tuple[ObjectId, bytes]]) -> None:
        small: list[tuple[ObjectId, bytes]] = []
        for oid, data in objects:
            if await self.has_object(oid):
                continue
            if should_chunk(data):
                await self.write_object(oid, data)
            else:
                small.append((oid, data))
        if not small:
            return
        if len(small) == 1:
            await self.write_object(*small[0])
            return
        await asyncio.to_thread(self._write_segment_sync, small)

    async def has_object(self, oid: ObjectId) -> bool:
        if self._object_path(oid).exists():
            try:
                data = await asyncio.to_thread(self._object_path(oid).read_bytes)
                return ObjectId.from_bytes(data) == oid
            except Exception:
                return False
        try:
            await asyncio.to_thread(self._read_chunked_object_sync, oid)
            return True
        except (ObjectNotFound, BackendUnavailable):
            pass
        return await asyncio.to_thread(self._segment_contains_sync, oid)

    async def read_ref(self, name: str) -> ObjectId | None:
        path = self._ref_path(name)
        if not path.exists():
            return None
        raw = (await asyncio.to_thread(path.read_text, encoding="utf-8")).strip()
        return ObjectId(raw) if raw else None

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        return await asyncio.to_thread(self._cas_ref_sync, name, expected, new)

    async def delete_ref(self, name: str) -> None:
        path = self._ref_path(name)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    async def delete_object(self, oid: ObjectId) -> None:
        path = self._object_path(oid)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    async def list_refs(self, prefix: str = "") -> AsyncIterator[Ref]:
        refs_root = self.root / "refs"
        if not refs_root.exists():
            return
        normalized_prefix = normalize_ref(prefix) if prefix else "refs/"
        for path in sorted(refs_root.rglob("*")):
            if not path.is_file():
                continue
            name = path.relative_to(self.root).as_posix()
            if name.startswith(normalized_prefix):
                raw = path.read_text(encoding="utf-8").strip()
                yield Ref(name, ObjectId(raw) if raw else None)

    async def append_journal(self, entry: JournalEntry) -> None:
        path = self.root / "journals" / f"{entry.id}.txn"
        if path.exists():
            return
        await asyncio.to_thread(self._write_atomic, path, entry.to_json().encode())

    async def read_config(self) -> dict[str, object]:
        path = self.root / "config"
        if not path.exists():
            raise ObjectNotFound("config")
        return json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))

    async def write_config(self, data: dict[str, object]) -> None:
        payload = json.dumps(data, sort_keys=True).encode()
        await asyncio.to_thread(self._write_atomic, self.root / "config", payload)

    def _object_path(self, oid: ObjectId) -> Path:
        return self.root / "objects" / oid.value[:2] / oid.value

    def _chunk_manifest_path(self, oid: ObjectId) -> Path:
        return self.root / "objects" / oid.value[:2] / f"{oid.value}.tmanifest"

    def _chunk_path(self, chunk_id: str) -> Path:
        return self.root / "chunks" / chunk_id[:2] / chunk_id

    def _segment_path(self, segment_id: str) -> Path:
        return self.root / "segments" / f"{segment_id}.tseg"

    def _segment_index_path(self, segment_id: str) -> Path:
        return self.root / "segments" / f"{segment_id}.tidx"

    def _ref_path(self, name: str) -> Path:
        return self.root / normalize_ref(name)

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _write_segment_sync(self, objects: list[tuple[ObjectId, bytes]]) -> None:
        segment_id, segment, index = encode_segment(objects)
        segment_path = self._segment_path(segment_id)
        index_path = self._segment_index_path(segment_id)
        if segment_path.exists() and index_path.exists():
            return
        self._write_atomic(segment_path, segment)
        self._write_atomic(index_path, index)

    def _write_chunked_object_sync(self, oid: ObjectId, data: bytes) -> None:
        manifest_path = self._chunk_manifest_path(oid)
        if manifest_path.exists():
            try:
                self._read_chunked_object_sync(oid)
                return
            except BackendUnavailable:
                pass
        manifest, chunks = encode_chunked_object(oid, data)
        for chunk_id, chunk in chunks:
            path = self._chunk_path(chunk_id)
            self._write_atomic(path, chunk)
        self._write_atomic(manifest_path, manifest)

    def _read_chunked_object_sync(self, oid: ObjectId) -> bytes:
        manifest_path = self._chunk_manifest_path(oid)
        if not manifest_path.exists():
            raise ObjectNotFound(str(oid))
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = decode_manifest(manifest_bytes)
            chunks = []
            for entry in manifest.chunks:
                chunk_path = self._chunk_path(entry.id)
                if not chunk_path.exists():
                    raise BackendUnavailable(f"missing chunk {entry.id} for object {oid}")
                chunks.append(chunk_path.read_bytes())
            return assemble_chunked_object(manifest_bytes, chunks)
        except BackendUnavailable:
            raise
        except ValueError as exc:
            raise BackendUnavailable(f"corrupt chunked object {oid}: {exc}") from exc

    def _read_segment_object_sync(self, oid: ObjectId) -> bytes:
        segments = self.root / "segments"
        if not segments.exists():
            raise ObjectNotFound(str(oid))
        for index_path in sorted(segments.glob("*.tidx")):
            segment_path = index_path.with_suffix(".tseg")
            if not segment_path.exists():
                raise BackendUnavailable(f"missing segment data for {index_path.name}")
            try:
                return read_segment_object(segment_path.read_bytes(), index_path.read_bytes(), oid)
            except ObjectNotFound:
                continue
            except ValueError as exc:
                raise BackendUnavailable(f"corrupt segment {index_path.name}: {exc}") from exc
        raise ObjectNotFound(str(oid))

    def _segment_contains_sync(self, oid: ObjectId) -> bool:
        try:
            self._read_segment_object_sync(oid)
        except ObjectNotFound:
            return False
        return True

    def _cas_ref_sync(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        with self._ref_lock(name):
            path = self._ref_path(name)
            current = None
            if path.exists():
                raw = path.read_text(encoding="utf-8").strip()
                current = ObjectId(raw) if raw else None
            if current != expected:
                return False
            self._write_atomic(path, f"{new}\n".encode())
            return True

    @contextlib.contextmanager
    def _ref_lock(self, name: str):
        key = hashlib.sha256(name.encode("utf-8")).hexdigest()
        path = self.root / "locks" / "refs" / f"{key}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            self._lock_file(handle)
            try:
                yield
            finally:
                self._unlock_file(handle)

    def _lock_file(self, handle) -> None:
        if os.name == "nt":
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock_file(self, handle) -> None:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
