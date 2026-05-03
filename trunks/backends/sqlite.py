from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator

from ..backend import Backend, Capabilities, Role
from ..errors import ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..url import BackendURL


class SQLite(Backend):
    def __init__(self, path: str | Path, *, trunk: str, role: Role = Role.primary) -> None:
        self.path = Path(path)
        self.trunk = trunk
        self.url = BackendURL.parse(self.path.as_posix())
        self.role = role

    @classmethod
    def from_url(cls, value: str) -> "SQLite":
        path, trunk = _split_sqlite_url(value)
        return cls(path, trunk=trunk)

    async def capabilities(self) -> Capabilities:
        return Capabilities(cas_refs=True, locks=False, journal=True, list_prefix=True, read_after_write_refs=True)

    async def read_object(self, oid: ObjectId) -> bytes:
        row = await asyncio.to_thread(
            self._fetchone,
            "select data from trunks_objects where trunk = ? and oid = ?",
            (self.trunk, str(oid)),
        )
        if row is None:
            raise ObjectNotFound(str(oid))
        return row[0]

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        await asyncio.to_thread(
            self._execute,
            "insert or ignore into trunks_objects(trunk, oid, data) values (?, ?, ?)",
            (self.trunk, str(oid), data),
        )

    async def read_ref(self, name: str) -> ObjectId | None:
        row = await asyncio.to_thread(
            self._fetchone,
            "select oid from trunks_refs where trunk = ? and name = ?",
            (self.trunk, normalize_ref(name)),
        )
        return ObjectId(row[0]) if row else None

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        return await asyncio.to_thread(self._cas_ref_sync, normalize_ref(name), expected, new)

    async def delete_ref(self, name: str) -> None:
        await asyncio.to_thread(
            self._execute,
            "delete from trunks_refs where trunk = ? and name = ?",
            (self.trunk, normalize_ref(name)),
        )

    async def delete_object(self, oid: ObjectId) -> None:
        await asyncio.to_thread(
            self._execute,
            "delete from trunks_objects where trunk = ? and oid = ?",
            (self.trunk, str(oid)),
        )

    async def list_refs(self, prefix: str = "") -> AsyncIterator[Ref]:
        normalized_prefix = normalize_ref(prefix) if prefix else ""
        rows = await asyncio.to_thread(
            self._fetchall,
            "select name, oid from trunks_refs where trunk = ? and name like ? order by name",
            (self.trunk, f"{normalized_prefix}%"),
        )
        for name, oid in rows:
            yield Ref(name, ObjectId(oid))

    async def append_journal(self, entry: JournalEntry) -> None:
        await asyncio.to_thread(
            self._execute,
            "insert or ignore into trunks_journals(trunk, id, data) values (?, ?, ?)",
            (self.trunk, entry.id, entry.to_json()),
        )

    async def read_config(self) -> dict[str, object]:
        row = await asyncio.to_thread(
            self._fetchone,
            "select data from trunks_config where trunk = ? and key = ?",
            (self.trunk, "storage"),
        )
        if row is None:
            raise ObjectNotFound("config")
        return json.loads(row[0])

    async def write_config(self, data: dict[str, object]) -> None:
        await asyncio.to_thread(
            self._execute,
            "insert into trunks_config(trunk, key, data) values (?, ?, ?) "
            "on conflict(trunk, key) do update set data = excluded.data",
            (self.trunk, "storage", json.dumps(data, sort_keys=True)),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("pragma journal_mode=wal")
            conn.execute(
                "create table if not exists trunks_objects("
                "trunk text not null, oid text not null, data blob not null, "
                "primary key (trunk, oid))"
            )
            conn.execute(
                "create table if not exists trunks_refs("
                "trunk text not null, name text not null, oid text not null, "
                "primary key (trunk, name))"
            )
            conn.execute(
                "create table if not exists trunks_journals("
                "trunk text not null, id text not null, data text not null, "
                "primary key (trunk, id))"
            )
            conn.execute(
                "create table if not exists trunks_config("
                "trunk text not null, key text not null, data text not null, "
                "primary key (trunk, key))"
            )
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _execute(self, sql: str, params: tuple[object, ...]) -> None:
        with self._connect() as conn:
            conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple[object, ...]) -> tuple | None:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple[object, ...]) -> list[tuple]:
        with self._connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def _cas_ref_sync(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select oid from trunks_refs where trunk = ? and name = ?",
                (self.trunk, name),
            ).fetchone()
            current = ObjectId(row[0]) if row else None
            if current != expected:
                return False
            conn.execute(
                "insert into trunks_refs(trunk, name, oid) values (?, ?, ?) "
                "on conflict(trunk, name) do update set oid = excluded.oid",
                (self.trunk, name, str(new)),
            )
            return True


def _split_sqlite_url(value: str) -> tuple[Path, str]:
    raw = value
    if raw.startswith("sqlite://"):
        raw = raw[len("sqlite://") :]
    raw = raw.lstrip("/")
    if "#" in raw:
        path_part, trunk = raw.rsplit("#", 1)
    else:
        if "/" not in raw:
            raise ValueError(
                "sqlite URL must include a trunk segment: "
                "sqlite:///path/to/file.db#<trunk-name> or sqlite:///path/to/file.db/<trunk-name>"
            )
        path_part, trunk = raw.rsplit("/", 1)
        if not path_part.endswith(".db") and not path_part.endswith(".sqlite") and not path_part.endswith(".sqlite3"):
            raise ValueError(
                "sqlite URL must point at a .db file with a trunk segment: "
                "sqlite:///path/to/file.db#<trunk-name>"
            )
    if not path_part or not trunk:
        raise ValueError("sqlite URL must include both a database path and a trunk segment")
    return Path("/" + path_part) if not path_part.startswith("/") else Path(path_part), trunk
