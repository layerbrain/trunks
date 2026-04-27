from __future__ import annotations

from dataclasses import dataclass

from .ids import ObjectId


def normalize_ref(name: str) -> str:
    if name in {"HEAD", ""}:
        return name
    if name.startswith("refs/"):
        return name
    if name.startswith("heads/"):
        return f"refs/{name}"
    if name.startswith("tags/"):
        return f"refs/{name}"
    return f"refs/heads/{name}"


def branch_name(ref: str) -> str:
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return ref


@dataclass(frozen=True)
class Ref:
    name: str
    oid: ObjectId | None


@dataclass(frozen=True)
class RefUpdate:
    name: str
    expected: ObjectId | None
    new: ObjectId


class RefStore:
    def __init__(self) -> None:
        self._refs: dict[str, ObjectId] = {}

    def get(self, name: str) -> ObjectId | None:
        return self._refs.get(normalize_ref(name))

    def set(self, name: str, oid: ObjectId) -> None:
        self._refs[normalize_ref(name)] = oid

    def cas(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        normalized = normalize_ref(name)
        if self._refs.get(normalized) != expected:
            return False
        self._refs[normalized] = new
        return True

    def list(self, prefix: str = "") -> list[Ref]:
        normalized_prefix = normalize_ref(prefix) if prefix else ""
        return [
            Ref(name, oid)
            for name, oid in sorted(self._refs.items())
            if name.startswith(normalized_prefix)
        ]

