from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

from ..backend import Backend, Capabilities, Role
from ..credentials import GCSCredentials
from ..errors import BackendUnavailable, ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..url import BackendURL


class GCS(Backend):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        project: str | None = None,
        endpoint: str | None = None,
        credentials: GCSCredentials | None = None,
        role: Role = Role.primary,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.project = project
        self.endpoint = endpoint
        self.credentials = credentials
        self.role = role
        raw = f"gcs://{bucket}/{self.prefix}" if self.prefix else f"gcs://{bucket}"
        self.url = BackendURL.parse(raw)
        self._client = None
        self._bucket_ready = False

    @classmethod
    def from_url(cls, value: str) -> "GCS":
        parsed = urlparse(value)
        return cls(bucket=parsed.netloc, prefix=parsed.path.strip("/"))

    async def capabilities(self) -> Capabilities:
        return Capabilities(cas_refs=True, locks=True, journal=True, list_prefix=True, read_after_write_refs=True)

    async def ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        await asyncio.to_thread(self._ensure_bucket_sync)
        self._bucket_ready = True

    async def __aenter__(self) -> "Backend":
        await self.ensure_bucket()
        return self

    async def read_object(self, oid: ObjectId) -> bytes:
        try:
            return await asyncio.to_thread(self._download, self._object_key(oid))
        except BackendUnavailable as exc:
            raise ObjectNotFound(str(oid)) from exc

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        try:
            await asyncio.to_thread(self._upload, self._object_key(oid), data, False)
        except BackendUnavailable as exc:
            if "Precondition" not in str(exc) and "already exists" not in str(exc).lower():
                raise

    async def read_ref(self, name: str) -> ObjectId | None:
        try:
            raw = (await asyncio.to_thread(self._download, self._key(normalize_ref(name)))).decode().strip()
        except BackendUnavailable:
            return None
        return ObjectId(raw) if raw else None

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        ref_name = normalize_ref(name)
        lock_key = self._key(f"locks/{quote(ref_name, safe='')}.lock")
        try:
            await asyncio.to_thread(self._upload, lock_key, f"{datetime.now(UTC).isoformat()}\n".encode(), False)
        except BackendUnavailable as exc:
            if "Precondition" in str(exc) or "already exists" in str(exc).lower():
                return False
            raise
        try:
            current = await self.read_ref(ref_name)
            if current != expected:
                return False
            await asyncio.to_thread(self._upload, self._key(ref_name), f"{new}\n".encode(), True)
            return True
        finally:
            await asyncio.to_thread(self._delete, lock_key)

    async def list_refs(self, prefix: str = ""):
        ref_prefix = self._key(normalize_ref(prefix) if prefix else "refs/")
        keys = await asyncio.to_thread(self._list_keys, ref_prefix)
        for key in keys:
            raw = (await asyncio.to_thread(self._download, key)).decode().strip()
            if raw:
                yield Ref(self._strip_prefix(key), ObjectId(raw))

    async def append_journal(self, entry: JournalEntry) -> None:
        try:
            await asyncio.to_thread(self._upload, self._key(f"journals/{entry.id}.txn"), entry.to_json().encode(), False)
        except BackendUnavailable as exc:
            if "Precondition" not in str(exc) and "already exists" not in str(exc).lower():
                raise

    async def read_config(self) -> dict[str, object]:
        try:
            return json.loads((await asyncio.to_thread(self._download, self._key("config"))).decode())
        except BackendUnavailable as exc:
            raise ObjectNotFound("config") from exc

    async def write_config(self, data: dict[str, object]) -> None:
        await asyncio.to_thread(self._upload, self._key("config"), json.dumps(data, sort_keys=True).encode(), True)

    async def __aexit__(self, *exc: object) -> None:
        self._client = None

    def _ensure_bucket_sync(self) -> None:
        client = self._storage_client()
        bucket = client.bucket(self.bucket)
        try:
            if not bucket.exists(client):
                bucket.create(client, project=self.project)
        except Exception as exc:
            if "already exists" not in str(exc).lower() and "409" not in str(exc):
                raise BackendUnavailable(str(exc)) from exc

    def _download(self, key: str) -> bytes:
        blob = self._blob(key)
        try:
            return blob.download_as_bytes()
        except Exception as exc:
            raise BackendUnavailable(str(exc)) from exc

    def _upload(self, key: str, data: bytes, overwrite: bool) -> None:
        blob = self._blob(key)
        try:
            kwargs = {} if overwrite else {"if_generation_match": 0}
            blob.upload_from_string(data, **kwargs)
        except Exception as exc:
            raise BackendUnavailable(str(exc)) from exc

    def _delete(self, key: str) -> None:
        try:
            self._blob(key).delete()
        except Exception:
            return

    def _list_keys(self, prefix: str) -> list[str]:
        client = self._storage_client()
        return [blob.name for blob in client.list_blobs(self.bucket, prefix=prefix)]

    def _blob(self, key: str):
        return self._storage_client().bucket(self.bucket).blob(key)

    def _storage_client(self):
        if self._client is not None:
            return self._client
        try:
            from google.auth.credentials import AnonymousCredentials
            from google.cloud import storage
            from google.oauth2 import service_account
        except ImportError as exc:
            raise BackendUnavailable("install trunks[gcs] to use gcs:// backends") from exc

        client_options = {"api_endpoint": self.endpoint} if self.endpoint else None
        if self.credentials is None:
            creds = AnonymousCredentials() if self.endpoint else None
            self._client = storage.Client(
                project=self.project or "trunks",
                credentials=creds,
                client_options=client_options,
            )
            return self._client

        source = self.credentials.service_account
        if isinstance(source, dict):
            creds = service_account.Credentials.from_service_account_info(source)
        else:
            creds = service_account.Credentials.from_service_account_file(str(source))
        self._client = storage.Client(project=self.project, credentials=creds, client_options=client_options)
        return self._client

    def _object_key(self, oid: ObjectId) -> str:
        return self._key(f"objects/{oid.value[:2]}/{oid.value}")

    def _key(self, suffix: str) -> str:
        return "/".join(part for part in [self.prefix, suffix.strip("/")] if part)

    def _strip_prefix(self, key: str) -> str:
        if self.prefix and key.startswith(f"{self.prefix}/"):
            return key[len(self.prefix) + 1 :]
        return key
