from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import AsyncIterator

from ..backend import Backend, Capabilities, Role
from ..errors import BackendUnavailable
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref
from ..url import BackendURL


class Multi(Backend):
    def __init__(
        self,
        *,
        primary: Backend,
        mirrors: Iterable[Backend] = (),
        archives: Iterable[Backend] = (),
        require_all: bool = True,
    ) -> None:
        self.primary = primary
        self.mirrors = list(mirrors)
        self.archives = list(archives)
        self.require_all = require_all
        self.role = Role.primary
        self.url = BackendURL.parse(primary.url.raw)
        self.mirror_failures: list[str] = []

    async def capabilities(self) -> Capabilities:
        return await self.primary.capabilities()

    async def read_object(self, oid: ObjectId) -> bytes:
        return await self.primary.read_object(oid)

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        await self.primary.write_object(oid, data)
        await self._write_object_replicas(oid, data)

    async def write_objects(self, objects):
        object_list = list(objects)
        await self.primary.write_objects(object_list)
        await self._write_objects_replicas(object_list)

    async def read_ref(self, name: str) -> ObjectId | None:
        return await self.primary.read_ref(name)

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        updated = await self.primary.cas_ref(name, expected, new)
        if not updated:
            return False
        await self._set_replica_refs(name, new)
        return True

    async def list_refs(self, prefix: str = "") -> AsyncIterator[Ref]:
        async for ref in self.primary.list_refs(prefix):
            yield ref

    async def append_journal(self, entry: JournalEntry) -> None:
        await self.primary.append_journal(entry)
        await self._append_replica_journals(entry)

    async def read_config(self) -> dict[str, object]:
        return await self.primary.read_config()

    async def write_config(self, data: dict[str, object]) -> None:
        await self.primary.write_config(data)
        for backend in [*self.mirrors, *self.archives]:
            try:
                await backend.write_config(data)
            except Exception as exc:
                self._record_replica_failure(f"{backend.url.raw}: write_config: {exc}")

    async def __aenter__(self) -> "Multi":
        await self.primary.__aenter__()
        for backend in [*self.mirrors, *self.archives]:
            await backend.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        for backend in reversed([self.primary, *self.mirrors, *self.archives]):
            with contextlib.suppress(Exception):
                await backend.__aexit__(*exc)

    async def _write_object_replicas(self, oid: ObjectId, data: bytes) -> None:
        for backend in [*self.mirrors, *self.archives]:
            try:
                await backend.write_object(oid, data)
            except Exception as exc:
                self._record_replica_failure(f"{backend.url.raw}: write_object {oid}: {exc}")

    async def _write_objects_replicas(self, objects) -> None:
        for backend in [*self.mirrors, *self.archives]:
            try:
                await backend.write_objects(objects)
            except Exception as exc:
                self._record_replica_failure(f"{backend.url.raw}: write_objects: {exc}")

    async def _set_replica_refs(self, name: str, new: ObjectId) -> None:
        for backend in self.mirrors:
            try:
                current = await backend.read_ref(name)
                if current != new:
                    await backend.cas_ref(name, current, new)
            except Exception as exc:
                self._record_replica_failure(f"{backend.url.raw}: cas_ref {name}: {exc}")

    async def _append_replica_journals(self, entry: JournalEntry) -> None:
        for backend in [*self.mirrors, *self.archives]:
            try:
                await backend.append_journal(entry)
            except Exception as exc:
                self._record_replica_failure(f"{backend.url.raw}: append_journal {entry.id}: {exc}")

    def _record_replica_failure(self, message: str) -> None:
        self.mirror_failures.append(message)
        if self.require_all:
            raise BackendUnavailable(message)
