from __future__ import annotations

import copy
from typing import AsyncIterator

from ..backend import Backend, Capabilities, Role
from ..errors import ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..url import BackendURL


class Memory(Backend):
    def __init__(self, *, role: Role = Role.primary) -> None:
        self.url = BackendURL.parse("memory://")
        self.role = role
        self.objects: dict[ObjectId, bytes] = {}
        self.refs: dict[str, ObjectId] = {}
        self.journals: dict[str, JournalEntry] = {}
        self.config: dict[str, object] | None = None

    async def capabilities(self) -> Capabilities:
        return Capabilities(cas_refs=True, locks=False, journal=True, list_prefix=True, read_after_write_refs=True)

    async def read_object(self, oid: ObjectId) -> bytes:
        try:
            return self.objects[oid]
        except KeyError as exc:
            raise ObjectNotFound(str(oid)) from exc

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        self.objects.setdefault(oid, data)

    async def read_ref(self, name: str) -> ObjectId | None:
        return self.refs.get(normalize_ref(name))

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        normalized = normalize_ref(name)
        if self.refs.get(normalized) != expected:
            return False
        self.refs[normalized] = new
        return True

    async def list_refs(self, prefix: str = "") -> AsyncIterator[Ref]:
        normalized_prefix = normalize_ref(prefix) if prefix else ""
        for name, oid in sorted(self.refs.items()):
            if name.startswith(normalized_prefix):
                yield Ref(name, oid)

    async def append_journal(self, entry: JournalEntry) -> None:
        self.journals.setdefault(entry.id, entry)

    async def read_config(self) -> dict[str, object]:
        if self.config is None:
            raise ObjectNotFound("config")
        return copy.deepcopy(self.config)

    async def write_config(self, data: dict[str, object]) -> None:
        self.config = copy.deepcopy(data)
