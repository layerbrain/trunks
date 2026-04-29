from __future__ import annotations

import json
import stat
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from ..backend import Backend, Capabilities, Role
from ..credentials import SFTPCredentials
from ..errors import BackendUnavailable, ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..url import BackendURL


class SFTP(Backend):
    def __init__(
        self,
        *,
        host: str,
        root: str,
        username: str | None = None,
        credentials: SFTPCredentials | None = None,
        port: int = 22,
        role: Role = Role.primary,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.credentials = credentials
        self.root = PurePosixPath(root)
        self.role = role
        self.url = BackendURL.parse(f"sftp://{host}{root}")
        self._conn = None
        self._sftp = None

    @classmethod
    def from_url(cls, value: str) -> "SFTP":
        parsed = urlparse(value)
        return cls(
            host=parsed.hostname or "localhost",
            port=parsed.port or 22,
            username=unquote(parsed.username) if parsed.username else None,
            credentials=SFTPCredentials.from_env(),
            root=parsed.path or "/trunks.trunk",
        )

    async def capabilities(self) -> Capabilities:
        return Capabilities(cas_refs=True, locks=True, journal=True, list_prefix=True, read_after_write_refs=True)

    async def read_object(self, oid: ObjectId) -> bytes:
        try:
            return await self._read(self._object_path(oid))
        except BackendUnavailable as exc:
            raise ObjectNotFound(str(oid)) from exc

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        path = self._object_path(oid)
        if await self._exists(path):
            return
        await self._write(path, data, overwrite=True)

    async def read_ref(self, name: str) -> ObjectId | None:
        try:
            raw = (await self._read(self._path(normalize_ref(name)))).decode().strip()
        except BackendUnavailable:
            return None
        return ObjectId(raw) if raw else None

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        ref_name = normalize_ref(name)
        lock = self._path(f"locks/{ref_name.replace('/', '%2F')}.lock")
        try:
            await self._write(lock, b"lock\n", overwrite=False)
        except BackendUnavailable:
            return False
        try:
            current = await self.read_ref(ref_name)
            if current != expected:
                return False
            await self._write(self._path(ref_name), f"{new}\n".encode(), overwrite=True)
            return True
        finally:
            await self._remove(lock)

    async def list_refs(self, prefix: str = ""):
        sftp = await self._client()
        refs_root = self._path("refs")
        normalized_prefix = normalize_ref(prefix) if prefix else "refs/"
        files: list[str] = []
        async for path in self._walk_files(sftp, refs_root):
            files.append(path)
        for path in sorted(files):
            relative = self._relative(path)
            if not relative.startswith(normalized_prefix):
                continue
            raw = (await self._read(path)).decode().strip()
            if raw:
                yield Ref(relative, ObjectId(raw))

    async def append_journal(self, entry: JournalEntry) -> None:
        try:
            await self._write(self._path(f"journals/{entry.id}.txn"), entry.to_json().encode(), overwrite=False)
        except BackendUnavailable as exc:
            if "exists" not in str(exc).lower():
                raise

    async def read_config(self) -> dict[str, object]:
        try:
            return json.loads((await self._read(self._path("config"))).decode())
        except BackendUnavailable as exc:
            raise ObjectNotFound("config") from exc

    async def write_config(self, data: dict[str, object]) -> None:
        await self._write(self._path("config"), json.dumps(data, sort_keys=True).encode(), overwrite=True)

    async def __aexit__(self, *exc: object) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
        self._conn = None
        self._sftp = None

    def _object_path(self, oid: ObjectId) -> str:
        return self._path(f"objects/{oid.value[:2]}/{oid.value}")

    def _path(self, suffix: str) -> str:
        return str(self.root / suffix.strip("/"))

    def _relative(self, path: str) -> str:
        root = str(self.root).rstrip("/") + "/"
        return path[len(root) :] if path.startswith(root) else path

    async def _client(self):
        if self._sftp is None:
            try:
                import asyncssh
            except ImportError as exc:
                raise BackendUnavailable("install trunks[sftp] to use sftp:// backends") from exc
            creds = self.credentials
            self._conn = await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                client_keys=[str(creds.ssh_key)] if creds and creds.ssh_key else None,
                passphrase=creds.passphrase if creds else None,
                password=creds.password if creds else None,
                known_hosts=None,
            )
            self._sftp = await self._conn.start_sftp_client()
            await self._sftp.makedirs(str(self.root), exist_ok=True)
        return self._sftp

    async def _exists(self, path: str) -> bool:
        sftp = await self._client()
        try:
            await sftp.stat(path)
            return True
        except Exception:
            return False

    async def _read(self, path: str) -> bytes:
        sftp = await self._client()
        try:
            async with sftp.open(path, "rb") as handle:
                return await handle.read()
        except Exception as exc:
            raise BackendUnavailable(str(exc)) from exc

    async def _write(self, path: str, data: bytes, *, overwrite: bool) -> None:
        sftp = await self._client()
        try:
            await sftp.makedirs(str(PurePosixPath(path).parent), exist_ok=True)
            mode = "wb" if overwrite else "xb"
            async with sftp.open(path, mode) as handle:
                await handle.write(data)
        except Exception as exc:
            raise BackendUnavailable(str(exc)) from exc

    async def _remove(self, path: str) -> None:
        sftp = await self._client()
        try:
            await sftp.remove(path)
        except Exception:
            return

    async def _walk_files(self, sftp, root: str):
        if not await self._exists(root):
            return
        stack: list[str] = [root]
        while stack:
            current = stack.pop()
            for entry in await sftp.readdir(current):
                name = entry.filename
                if name in {".", ".."}:
                    continue
                child = f"{current.rstrip('/')}/{name}"
                if stat.S_ISDIR(entry.attrs.permissions):
                    stack.append(child)
                else:
                    yield child
