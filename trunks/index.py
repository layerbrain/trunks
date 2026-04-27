from __future__ import annotations

from dataclasses import dataclass

from .ids import ObjectId


@dataclass(frozen=True)
class IndexEntry:
    path: str
    oid: ObjectId
    mode: str = "100644"


class Index:
    def __init__(self, entries: list[IndexEntry] | None = None) -> None:
        self._entries = {entry.path: entry for entry in entries or []}

    def set(self, entry: IndexEntry) -> None:
        self._entries[entry.path] = entry

    def delete(self, path: str) -> None:
        self._entries.pop(path, None)

    def get(self, path: str) -> IndexEntry | None:
        return self._entries.get(path)

    def entries(self) -> list[IndexEntry]:
        return [self._entries[path] for path in sorted(self._entries)]

    def paths(self) -> set[str]:
        return set(self._entries)

