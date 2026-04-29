from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import AsyncIterator

from ..backend import Backend, Capabilities, Role
from ..errors import ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref
from ..url import BackendURL
from .manager import CacheManager


class CachedBackend(Backend):
    def __init__(self, backend: Backend, cache: CacheManager | None = None) -> None:
        self.backend = backend
        namespace = hashlib.sha1(str(backend.url).encode()).hexdigest()
        self.cache = cache or CacheManager(CacheManager.default_root() / "backends" / namespace)
        self.url: BackendURL = backend.url
        self.role: Role = backend.role

    async def capabilities(self) -> Capabilities:
        return await self.backend.capabilities()

    async def read_object(self, oid: ObjectId) -> bytes:
        cached = self.cache.get_object(oid)
        if cached is not None:
            return cached
        if self.cache.has_negative(oid):
            raise ObjectNotFound(str(oid))
        try:
            data = await self.backend.read_object(oid)
        except ObjectNotFound:
            self.cache.put_negative(oid)
            raise
        self.cache.put_object(oid, data)
        return data

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        await self.backend.write_object(oid, data)
        self.cache.put_object(oid, data)

    async def write_objects(self, objects: Iterable[tuple[ObjectId, bytes]]) -> None:
        values = list(objects)
        await self.backend.write_objects(values)
        for oid, data in values:
            self.cache.put_object(oid, data)

    async def has_object(self, oid: ObjectId) -> bool:
        if self.cache.get_object(oid) is not None:
            return True
        if self.cache.has_negative(oid):
            return False
        return await self.backend.has_object(oid)

    async def read_ref(self, name: str) -> ObjectId | None:
        oid = await self.backend.read_ref(name)
        self.cache.put_ref(name, oid)
        return oid

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        updated = await self.backend.cas_ref(name, expected, new)
        if updated:
            self.cache.put_ref(name, new)
        else:
            self.cache.clear_refs()
        return updated

    async def list_refs(self, prefix: str = "") -> AsyncIterator[Ref]:
        async for ref in self.backend.list_refs(prefix):
            self.cache.put_ref(ref.name, ref.oid)
            yield ref

    async def append_journal(self, entry: JournalEntry) -> None:
        await self.backend.append_journal(entry)

    async def read_config(self) -> dict[str, object]:
        return await self.backend.read_config()

    async def write_config(self, data: dict[str, object]) -> None:
        await self.backend.write_config(data)

    async def __aenter__(self) -> "CachedBackend":
        await self.backend.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.backend.__aexit__(*exc)
