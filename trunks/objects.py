from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Iterable

from .errors import ObjectNotFound
from .ids import ObjectId


def canonical_object(kind: str, payload: bytes) -> bytes:
    return f"{kind} {len(payload)}\0".encode() + payload


def parse_canonical_object(data: bytes) -> tuple[str, bytes]:
    header, payload = data.split(b"\0", 1)
    kind, raw_size = header.decode().split(" ", 1)
    size = int(raw_size)
    if size != len(payload):
        raise ValueError(f"object size mismatch: expected {size}, got {len(payload)}")
    return kind, payload


def object_id(kind: str, payload: bytes) -> ObjectId:
    return ObjectId.from_bytes(canonical_object(kind, payload))


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    when: datetime = field(default_factory=lambda: datetime.now(UTC))

    def git(self) -> str:
        when = self.when
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        timestamp = int(when.timestamp())
        offset = when.utcoffset()
        if offset is None:
            offset_seconds = 0
        else:
            offset_seconds = int(offset.total_seconds())
        sign = "+" if offset_seconds >= 0 else "-"
        offset_seconds = abs(offset_seconds)
        hours, remainder = divmod(offset_seconds, 3600)
        minutes = remainder // 60
        return f"{self.name} <{self.email}> {timestamp} {sign}{hours:02d}{minutes:02d}"


@dataclass(frozen=True)
class Blob:
    id: ObjectId
    data: bytes

    @classmethod
    def from_data(cls, data: bytes) -> "Blob":
        return cls(object_id("blob", data), data)

    def canonical(self) -> bytes:
        return canonical_object("blob", self.data)


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    oid: ObjectId


@dataclass(frozen=True)
class Tree:
    id: ObjectId
    entries: tuple[TreeEntry, ...]

    @classmethod
    def from_entries(cls, entries: Iterable[TreeEntry]) -> "Tree":
        normalized = tuple(sorted(entries, key=lambda e: e.name.encode()))
        payload = serialize_tree_entries(normalized)
        return cls(object_id("tree", payload), normalized)

    def canonical(self) -> bytes:
        return canonical_object("tree", serialize_tree_entries(self.entries))


@dataclass(frozen=True)
class Commit:
    id: ObjectId
    tree: ObjectId
    parents: tuple[ObjectId, ...]
    author: Identity
    committer: Identity
    message: str

    @classmethod
    def create(
        cls,
        *,
        tree: ObjectId,
        parents: Iterable[ObjectId],
        author: Identity,
        committer: Identity,
        message: str,
    ) -> "Commit":
        parent_tuple = tuple(parents)
        payload = serialize_commit_payload(
            tree=tree,
            parents=parent_tuple,
            author=author,
            committer=committer,
            message=message,
        )
        return cls(object_id("commit", payload), tree, parent_tuple, author, committer, message)

    def canonical(self) -> bytes:
        return canonical_object(
            "commit",
            serialize_commit_payload(
                tree=self.tree,
                parents=self.parents,
                author=self.author,
                committer=self.committer,
                message=self.message,
            ),
        )


@dataclass(frozen=True)
class CommitRecord:
    commit: Commit
    labels: dict[str, str]


@dataclass(frozen=True)
class Tag:
    name: str
    oid: ObjectId


def serialize_tree_entries(entries: Iterable[TreeEntry]) -> bytes:
    parts: list[bytes] = []
    for entry in entries:
        parts.append(f"{entry.mode} {entry.name}\0".encode() + entry.oid.raw())
    return b"".join(parts)


def parse_tree_payload(payload: bytes) -> Tree:
    entries: list[TreeEntry] = []
    pos = 0
    while pos < len(payload):
        header_end = payload.index(b"\0", pos)
        mode_name = payload[pos:header_end].decode()
        mode, name = mode_name.split(" ", 1)
        oid = ObjectId(payload[header_end + 1 : header_end + 21].hex())
        entries.append(TreeEntry(mode, name, oid))
        pos = header_end + 21
    return Tree(object_id("tree", payload), tuple(entries))


def serialize_commit_payload(
    *,
    tree: ObjectId,
    parents: Iterable[ObjectId],
    author: Identity,
    committer: Identity,
    message: str,
) -> bytes:
    lines = [f"tree {tree}"]
    lines.extend(f"parent {parent}" for parent in parents)
    lines.append(f"author {author.git()}")
    lines.append(f"committer {committer.git()}")
    lines.append("")
    lines.append(message.rstrip("\n"))
    return ("\n".join(lines) + "\n").encode()


def parse_commit_payload(payload: bytes, oid: ObjectId) -> Commit:
    text = payload.decode()
    header, _, message = text.partition("\n\n")
    tree: ObjectId | None = None
    parents: list[ObjectId] = []
    author: Identity | None = None
    committer: Identity | None = None
    for line in header.splitlines():
        key, value = line.split(" ", 1)
        if key == "tree":
            tree = ObjectId(value)
        elif key == "parent":
            parents.append(ObjectId(value))
        elif key == "author":
            author = parse_identity(value)
        elif key == "committer":
            committer = parse_identity(value)
    if tree is None or author is None or committer is None:
        raise ValueError("invalid commit payload")
    return Commit(oid, tree, tuple(parents), author, committer, message.rstrip("\n"))


def parse_identity(value: str) -> Identity:
    name_email, raw_ts, raw_offset = value.rsplit(" ", 2)
    name, email = name_email.rsplit(" <", 1)
    email = email.rstrip(">")
    sign = 1 if raw_offset[0] == "+" else -1
    hours = int(raw_offset[1:3])
    minutes = int(raw_offset[3:5])
    offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
    when = datetime.fromtimestamp(int(raw_ts), offset)
    return Identity(name, email, when)


def object_kind(data: bytes) -> str:
    return parse_canonical_object(data)[0]


def require_object(objects: dict[ObjectId, bytes], oid: ObjectId) -> bytes:
    try:
        return objects[oid]
    except KeyError as exc:
        raise ObjectNotFound(str(oid)) from exc
