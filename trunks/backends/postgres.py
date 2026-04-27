from __future__ import annotations

import json
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from ..backend import Backend, Capabilities, Role
from ..credentials import PostgresCredentials
from ..errors import BackendUnavailable, ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..url import BackendURL


class Postgres(Backend):
    def __init__(
        self,
        credentials: PostgresCredentials,
        *,
        trunk: str,
        role: Role = Role.primary,
    ) -> None:
        self.credentials = credentials
        self.dsn = credentials.dsn
        self.trunk = trunk
        self.role = role
        self.url = BackendURL.parse(credentials.dsn)
        self._pool = None

    @classmethod
    def from_url(cls, value: str) -> "Postgres":
        dsn, trunk = _split_postgres_url(value)
        return cls(PostgresCredentials(dsn), trunk=trunk)

    async def capabilities(self) -> Capabilities:
        await self._ensure_schema()
        return Capabilities(cas_refs=True, locks=False, journal=True, list_prefix=True, read_after_write_refs=True)

    async def read_object(self, oid: ObjectId) -> bytes:
        pool = await self._pool_or_open()
        row = await pool.fetchrow(
            "select data from trunks_objects where trunk = $1 and oid = $2",
            self.trunk,
            str(oid),
        )
        if row is None:
            raise ObjectNotFound(str(oid))
        return bytes(row["data"])

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        pool = await self._pool_or_open()
        await pool.execute(
            "insert into trunks_objects(trunk, oid, data) values ($1, $2, $3) on conflict do nothing",
            self.trunk,
            str(oid),
            data,
        )

    async def read_ref(self, name: str) -> ObjectId | None:
        pool = await self._pool_or_open()
        row = await pool.fetchrow(
            "select oid from trunks_refs where trunk = $1 and name = $2",
            self.trunk,
            normalize_ref(name),
        )
        return ObjectId(row["oid"]) if row else None

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        ref = normalize_ref(name)
        pool = await self._pool_or_open()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "select pg_advisory_xact_lock(hashtext($1))",
                    f"{self.trunk}:{ref}",
                )
                row = await conn.fetchrow(
                    "select oid from trunks_refs where trunk = $1 and name = $2",
                    self.trunk,
                    ref,
                )
                current = ObjectId(row["oid"]) if row else None
                if current != expected:
                    return False
                await conn.execute(
                    "insert into trunks_refs(trunk, name, oid) values ($1, $2, $3) "
                    "on conflict(trunk, name) do update set oid = excluded.oid",
                    self.trunk,
                    ref,
                    str(new),
                )
                return True

    async def list_refs(self, prefix: str = "") -> AsyncIterator[Ref]:
        normalized_prefix = normalize_ref(prefix) if prefix else ""
        pool = await self._pool_or_open()
        rows = await pool.fetch(
            "select name, oid from trunks_refs where trunk = $1 and name like $2 order by name",
            self.trunk,
            f"{normalized_prefix}%",
        )
        for row in rows:
            yield Ref(row["name"], ObjectId(row["oid"]))

    async def append_journal(self, entry: JournalEntry) -> None:
        pool = await self._pool_or_open()
        await pool.execute(
            "insert into trunks_journals(trunk, id, data) values ($1, $2, $3) on conflict do nothing",
            self.trunk,
            entry.id,
            entry.to_json(),
        )

    async def read_config(self) -> dict[str, object]:
        pool = await self._pool_or_open()
        row = await pool.fetchrow(
            "select data from trunks_config where trunk = $1 and key = $2",
            self.trunk,
            "storage",
        )
        if row is None:
            raise ObjectNotFound("config")
        return json.loads(row["data"])

    async def write_config(self, data: dict[str, object]) -> None:
        pool = await self._pool_or_open()
        await pool.execute(
            "insert into trunks_config(trunk, key, data) values ($1, $2, $3) "
            "on conflict(trunk, key) do update set data = excluded.data",
            self.trunk,
            "storage",
            json.dumps(data, sort_keys=True),
        )

    async def __aexit__(self, *exc: object) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _pool_or_open(self):
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:
                raise BackendUnavailable("install trunks[postgres] to use postgres:// backends") from exc
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
            await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        pool = await self._pool_or_open_without_schema()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                create table if not exists trunks_objects(
                    trunk text not null,
                    oid text not null,
                    data bytea not null,
                    primary key (trunk, oid)
                );
                create table if not exists trunks_refs(
                    trunk text not null,
                    name text not null,
                    oid text not null,
                    primary key (trunk, name)
                );
                create table if not exists trunks_journals(
                    trunk text not null,
                    id text not null,
                    data text not null,
                    primary key (trunk, id)
                );
                create table if not exists trunks_config(
                    trunk text not null,
                    key text not null,
                    data text not null,
                    primary key (trunk, key)
                );
                """
            )

    async def _pool_or_open_without_schema(self):
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:
                raise BackendUnavailable("install trunks[postgres] to use postgres:// backends") from exc
            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        return self._pool


def _split_postgres_url(value: str) -> tuple[str, str]:
    parts = urlsplit(value)
    path = parts.path.lstrip("/")
    if "/" not in path:
        raise ValueError(
            "postgres URL must include a trunk path: "
            "postgres://user:pw@host/<database>/<trunk-name>"
        )
    database, trunk = path.split("/", 1)
    trunk = trunk.rstrip("/")
    if not database or not trunk:
        raise ValueError(
            "postgres URL must include a trunk path: "
            "postgres://user:pw@host/<database>/<trunk-name>"
        )
    dsn = urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))
    return dsn, trunk
