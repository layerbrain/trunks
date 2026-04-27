from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import AsyncIterator

from .errors import ObjectNotFound
from .ids import ObjectId
from .journal import JournalEntry
from .refs import Ref
from .url import BackendURL


class Role(StrEnum):
    primary = "primary"
    mirror = "mirror"
    archive = "archive"


@dataclass(frozen=True)
class Capabilities:
    cas_refs: bool
    locks: bool
    journal: bool
    list_prefix: bool
    read_after_write_refs: bool


class Backend(ABC):
    url: BackendURL
    role: Role

    @abstractmethod
    async def capabilities(self) -> Capabilities: ...

    @abstractmethod
    async def read_object(self, oid: ObjectId) -> bytes: ...

    @abstractmethod
    async def write_object(self, oid: ObjectId, data: bytes) -> None: ...

    async def write_objects(self, objects: Iterable[tuple[ObjectId, bytes]]) -> None:
        for oid, data in objects:
            await self.write_object(oid, data)

    async def has_object(self, oid: ObjectId) -> bool:
        try:
            await self.read_object(oid)
        except ObjectNotFound:
            return False
        return True

    @abstractmethod
    async def read_ref(self, name: str) -> ObjectId | None: ...

    @abstractmethod
    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool: ...

    @abstractmethod
    def list_refs(self, prefix: str = "") -> AsyncIterator[Ref]: ...

    @abstractmethod
    async def append_journal(self, entry: JournalEntry) -> None: ...

    @abstractmethod
    async def read_config(self) -> dict[str, object]: ...

    @abstractmethod
    async def write_config(self, data: dict[str, object]) -> None: ...

    async def __aenter__(self) -> "Backend":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None
