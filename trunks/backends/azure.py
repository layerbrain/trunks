from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

from ..backend import Backend, Capabilities, Role
from ..credentials import AzureCredentials
from ..errors import BackendUnavailable, ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..url import BackendURL


class Azure(Backend):
    def __init__(
        self,
        *,
        account: str,
        container: str,
        prefix: str = "",
        endpoint: str | None = None,
        credentials: AzureCredentials | None = None,
        role: Role = Role.primary,
    ) -> None:
        self.account = account
        self.container = container
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint or os.environ.get("AZURE_STORAGE_ENDPOINT") or f"https://{account}.blob.core.windows.net"
        self.credentials = credentials or AzureCredentials(
            account_name=account,
            account_key=os.environ.get("AZURE_STORAGE_KEY"),
            sas_token=os.environ.get("AZURE_STORAGE_SAS_TOKEN"),
        )
        self.role = role
        self.url = BackendURL.parse(f"azure://{account}/{container}/{self.prefix}")
        self._service = None
        self._container_ready = False

    @classmethod
    def from_url(cls, value: str) -> "Azure":
        parsed = urlparse(value)
        account = parsed.netloc
        parts = parsed.path.strip("/").split("/", 1)
        container = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        return cls(account=account, container=container, prefix=prefix)

    async def capabilities(self) -> Capabilities:
        return Capabilities(cas_refs=True, locks=True, journal=True, list_prefix=True, read_after_write_refs=True)

    async def ensure_container(self) -> None:
        if self._container_ready:
            return
        client = await self._container()
        try:
            await client.create_container()
        except Exception as exc:
            if "ContainerAlreadyExists" not in str(exc) and "ContainerBeingDeleted" not in str(exc):
                raise BackendUnavailable(str(exc)) from exc
        self._container_ready = True

    async def __aenter__(self) -> "Backend":
        await self.ensure_container()
        return self

    async def read_object(self, oid: ObjectId) -> bytes:
        try:
            return await self._download(self._object_key(oid))
        except BackendUnavailable as exc:
            raise ObjectNotFound(str(oid)) from exc

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        try:
            await self._upload(self._object_key(oid), data, overwrite=False)
        except BackendUnavailable as exc:
            if "BlobAlreadyExists" not in str(exc):
                raise

    async def read_ref(self, name: str) -> ObjectId | None:
        try:
            raw = (await self._download(self._key(normalize_ref(name)))).decode().strip()
        except BackendUnavailable:
            return None
        return ObjectId(raw) if raw else None

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        ref_name = normalize_ref(name)
        lock_key = self._key(f"locks/{quote(ref_name, safe='')}.lock")
        try:
            await self._upload(lock_key, f"{datetime.now(UTC).isoformat()}\n".encode(), overwrite=False)
        except BackendUnavailable as exc:
            if "BlobAlreadyExists" in str(exc):
                return False
            raise
        try:
            current = await self.read_ref(ref_name)
            if current != expected:
                return False
            await self._upload(self._key(ref_name), f"{new}\n".encode(), overwrite=True)
            return True
        finally:
            await self._delete(lock_key)

    async def list_refs(self, prefix: str = ""):
        client = await self._container()
        ref_prefix = self._key(normalize_ref(prefix) if prefix else "refs/")
        async for blob in client.list_blobs(name_starts_with=ref_prefix):
            raw = (await self._download(blob.name)).decode().strip()
            if raw:
                yield Ref(self._strip_prefix(blob.name), ObjectId(raw))

    async def append_journal(self, entry: JournalEntry) -> None:
        try:
            await self._upload(self._key(f"journals/{entry.id}.txn"), entry.to_json().encode(), overwrite=False)
        except BackendUnavailable as exc:
            if "BlobAlreadyExists" not in str(exc):
                raise

    async def read_config(self) -> dict[str, object]:
        try:
            return json.loads((await self._download(self._key("config"))).decode())
        except BackendUnavailable as exc:
            raise ObjectNotFound("config") from exc

    async def write_config(self, data: dict[str, object]) -> None:
        await self._upload(self._key("config"), json.dumps(data, sort_keys=True).encode(), overwrite=True)

    async def __aexit__(self, *exc: object) -> None:
        if self._service is not None:
            await self._service.close()
            self._service = None

    async def _container(self):
        service = await self._service_client()
        return service.get_container_client(self.container)

    async def _service_client(self):
        if self._service is None:
            try:
                from azure.storage.blob.aio import BlobServiceClient
            except ImportError as exc:
                raise BackendUnavailable("install trunks[azure] to use azure:// backends") from exc
            credential = self.credentials.sas_token or self.credentials.account_key
            if not credential:
                raise BackendUnavailable("missing Azure account key or SAS token")
            self._service = BlobServiceClient(account_url=self.endpoint, credential=credential)
        return self._service

    async def _download(self, key: str) -> bytes:
        client = (await self._container()).get_blob_client(key)
        try:
            stream = await client.download_blob()
            return await stream.readall()
        except Exception as exc:
            raise BackendUnavailable(str(exc)) from exc

    async def _upload(self, key: str, data: bytes, *, overwrite: bool) -> None:
        client = (await self._container()).get_blob_client(key)
        try:
            await client.upload_blob(data, overwrite=overwrite)
        except Exception as exc:
            raise BackendUnavailable(str(exc)) from exc

    async def _delete(self, key: str) -> None:
        client = (await self._container()).get_blob_client(key)
        try:
            await client.delete_blob()
        except Exception:
            return

    def _object_key(self, oid: ObjectId) -> str:
        return self._key(f"objects/{oid.value[:2]}/{oid.value}")

    def _key(self, suffix: str) -> str:
        return "/".join(part for part in [self.prefix, suffix.strip("/")] if part)

    def _strip_prefix(self, key: str) -> str:
        if self.prefix and key.startswith(f"{self.prefix}/"):
            return key[len(self.prefix) + 1 :]
        return key
