from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

from ..backend import Backend, Capabilities, Role
from ..credentials import S3Credentials
from ..chunked import assemble_chunked_object, decode_manifest, encode_chunked_object, should_chunk
from ..errors import BackendUnavailable, ObjectNotFound
from ..ids import ObjectId
from ..journal import JournalEntry
from ..refs import Ref, normalize_ref
from ..segment import decode_index, encode_segment, read_segment_object
from ..url import BackendURL, s3_scheme_defaults


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class S3(Backend):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint: str | None = None,
        region: str | None = None,
        credentials: S3Credentials | None = None,
        role: Role = Role.primary,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint
        self.region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self.credentials = credentials or S3Credentials.from_env("s3")
        self.role = role
        raw = f"s3://{bucket}/{self.prefix}" if self.prefix else f"s3://{bucket}"
        self.url = BackendURL.parse(raw)
        self._bucket_ready = False

    @classmethod
    def from_url(cls, value: str) -> "S3":
        endpoint = os.environ.get("AWS_ENDPOINT_URL") or os.environ.get("TRUNKS_S3_ENDPOINT")
        region = os.environ.get("TRUNKS_S3_REGION") or os.environ.get("AWS_REGION")
        defaults = s3_scheme_defaults(value, endpoint=endpoint, region=region)
        return cls(
            bucket=defaults.bucket,
            prefix=defaults.prefix,
            endpoint=defaults.endpoint,
            region=defaults.region,
            credentials=S3Credentials.from_env(defaults.credentials_scheme),
        )

    async def capabilities(self) -> Capabilities:
        return Capabilities(cas_refs=True, locks=True, journal=True, list_prefix=True, read_after_write_refs=True)

    async def read_object(self, oid: ObjectId) -> bytes:
        try:
            return await self._request("GET", self._object_key(oid), None, {})
        except BackendUnavailable as exc:
            if "404" not in str(exc):
                raise
        try:
            return await self._read_chunked_object(oid)
        except ObjectNotFound:
            pass
        return await self._read_segment_object(oid)

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        if should_chunk(data):
            await self._write_chunked_object(oid, data)
            return
        await self._request("PUT", self._object_key(oid), data, {})

    async def write_objects(self, objects: Iterable[tuple[ObjectId, bytes]]) -> None:
        small: list[tuple[ObjectId, bytes]] = []
        for oid, data in objects:
            if should_chunk(data):
                await self.write_object(oid, data)
            else:
                small.append((oid, data))
        if not small:
            return
        if len(small) == 1:
            await self.write_object(*small[0])
            return
        segment_id, segment, index = encode_segment(small)
        await self._request("PUT", self._segment_data_key(segment_id), segment, {})
        await self._request("PUT", self._segment_index_key(segment_id), index, {})

    async def read_ref(self, name: str) -> ObjectId | None:
        try:
            data = await self._request("GET", self._key(normalize_ref(name)), None, {})
        except BackendUnavailable as exc:
            if "404" in str(exc):
                return None
            raise
        raw = data.decode().strip()
        return ObjectId(raw) if raw else None

    async def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        ref_name = normalize_ref(name)
        lock_key = self._key(f"locks/{quote(ref_name, safe='')}.lock")
        try:
            await self._request(
                "PUT",
                lock_key,
                f"{datetime.now(UTC).isoformat()}\n".encode(),
                {"If-None-Match": "*"},
            )
        except BackendUnavailable as exc:
            if "412" in str(exc):
                return False
            raise
        try:
            current = await self.read_ref(ref_name)
            if current != expected:
                return False
            await self._request("PUT", self._key(ref_name), f"{new}\n".encode(), {})
            return True
        finally:
            await self._request("DELETE", lock_key, None, {})

    async def list_refs(self, prefix: str = ""):
        ref_prefix = self._key(normalize_ref(prefix) if prefix else "refs/")
        keys = await self._list_keys(ref_prefix)
        for key in keys:
            if key.endswith("/"):
                continue
            data = await self._request("GET", key, None, {})
            raw = data.decode().strip()
            if not raw:
                continue
            yield Ref(self._strip_prefix(key), ObjectId(raw))

    async def append_journal(self, entry: JournalEntry) -> None:
        try:
            await self._request("PUT", self._key(f"journals/{entry.id}.txn"), entry.to_json().encode(), {"If-None-Match": "*"})
        except BackendUnavailable as exc:
            if "412" not in str(exc):
                raise

    async def read_config(self) -> dict[str, object]:
        try:
            return json.loads((await self._request("GET", self._key("config"), None, {})).decode())
        except BackendUnavailable as exc:
            if "404" in str(exc):
                raise ObjectNotFound("config") from exc
            raise

    async def write_config(self, data: dict[str, object]) -> None:
        await self._request("PUT", self._key("config"), json.dumps(data, sort_keys=True).encode(), {})

    def _object_key(self, oid: ObjectId) -> str:
        return self._key(f"objects/{oid.value[:2]}/{oid.value}")

    def _chunk_manifest_key(self, oid: ObjectId) -> str:
        return self._key(f"objects/{oid.value[:2]}/{oid.value}.tmanifest")

    def _chunk_key(self, chunk_id: str) -> str:
        return self._key(f"chunks/{chunk_id[:2]}/{chunk_id}")

    def _segment_data_key(self, segment_id: str) -> str:
        return self._key(f"segments/{segment_id}.tseg")

    def _segment_index_key(self, segment_id: str) -> str:
        return self._key(f"segments/{segment_id}.tidx")

    def _key(self, suffix: str) -> str:
        return "/".join(part for part in [self.prefix, suffix.strip("/")] if part)

    def _strip_prefix(self, key: str) -> str:
        if self.prefix and key.startswith(f"{self.prefix}/"):
            return key[len(self.prefix) + 1 :]
        return key

    async def ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        try:
            await self._request("PUT", "", None, {})
        except BackendUnavailable as exc:
            if "409" not in str(exc):
                raise
        self._bucket_ready = True

    async def __aenter__(self) -> "Backend":
        await self.ensure_bucket()
        return self

    async def _read_segment_object(self, oid: ObjectId) -> bytes:
        keys = await self._list_keys(self._key("segments/"))
        for index_key in sorted(key for key in keys if key.endswith(".tidx")):
            index = await self._request("GET", index_key, None, {})
            decoded = decode_index(index)
            entry = next((item for item in decoded.entries if item.oid == oid), None)
            if entry is None:
                continue
            data_key = index_key[:-5] + ".tseg"
            try:
                segment = await self._request("GET", data_key, None, {})
            except BackendUnavailable as exc:
                raise BackendUnavailable(f"missing segment data for {index_key}") from exc
            try:
                return read_segment_object(segment, index, oid)
            except ValueError as exc:
                raise BackendUnavailable(f"corrupt segment {index_key}: {exc}") from exc
        raise ObjectNotFound(str(oid))

    async def _write_chunked_object(self, oid: ObjectId, data: bytes) -> None:
        manifest, chunks = encode_chunked_object(oid, data)
        for chunk_id, chunk in chunks:
            await self._request("PUT", self._chunk_key(chunk_id), chunk, {})
        await self._request("PUT", self._chunk_manifest_key(oid), manifest, {})

    async def _read_chunked_object(self, oid: ObjectId) -> bytes:
        try:
            manifest_bytes = await self._request("GET", self._chunk_manifest_key(oid), None, {})
        except BackendUnavailable as exc:
            if "404" in str(exc):
                raise ObjectNotFound(str(oid)) from exc
            raise
        try:
            manifest = decode_manifest(manifest_bytes)
            chunks = []
            for entry in manifest.chunks:
                try:
                    chunks.append(await self._request("GET", self._chunk_key(entry.id), None, {}))
                except BackendUnavailable as exc:
                    raise BackendUnavailable(f"missing chunk {entry.id} for object {oid}") from exc
            return assemble_chunked_object(manifest_bytes, chunks)
        except ValueError as exc:
            raise BackendUnavailable(f"corrupt chunked object {oid}: {exc}") from exc

    def _base_url(self, key: str) -> str:
        if self.endpoint:
            return f"{self.endpoint.rstrip('/')}/{self.bucket}/{quote(key, safe='/')}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{quote(key, safe='/')}"

    async def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {
                "list-type": "2",
                "prefix": prefix,
            }
            if token:
                query["continuation-token"] = token
            data = await self._request("GET", "", None, {}, query=query)
            root = ET.fromstring(data)
            namespace = ""
            if root.tag.startswith("{"):
                namespace = root.tag.split("}", 1)[0] + "}"
            for item in root.findall(f"{namespace}Contents"):
                key = item.findtext(f"{namespace}Key")
                if key:
                    keys.append(key)
            truncated = root.findtext(f"{namespace}IsTruncated") == "true"
            token = root.findtext(f"{namespace}NextContinuationToken")
            if not truncated or not token:
                return keys

    async def _request(
        self,
        method: str,
        key: str,
        body: bytes | None,
        headers: dict[str, str],
        *,
        query: dict[str, str] | None = None,
    ) -> bytes:
        body = body or b""
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        parsed = urlparse(self._base_url(key))
        host = parsed.netloc
        path = parsed.path or "/"
        canonical_query = self._canonical_query(query or {})
        signed_headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            **{k.lower(): v for k, v in headers.items()},
        }
        if self.credentials.session_token:
            signed_headers["x-amz-security-token"] = self.credentials.session_token
        header_names = ";".join(sorted(signed_headers))
        canonical_headers = "".join(f"{name}:{signed_headers[name]}\n" for name in sorted(signed_headers))
        canonical_request = "\n".join([method, path, canonical_query, canonical_headers, header_names, payload_hash])
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = hmac.new(self._signing_key(date_stamp), string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.credentials.access_key}/{scope}, "
            f"SignedHeaders={header_names}, Signature={signature}"
        )
        request_headers = {k: v for k, v in signed_headers.items() if k != "host"}
        request_headers["Authorization"] = authorization
        url = self._base_url(key)
        if canonical_query:
            url = f"{url}?{canonical_query}"
        try:
            import httpx
        except ImportError as exc:
            raise BackendUnavailable("install trunks[s3] to use S3-compatible backends") from exc
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.request(
                        method,
                        url,
                        content=body if method not in {"GET", "DELETE"} else None,
                        headers=request_headers,
                    )
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in RETRYABLE_STATUS_CODES and attempt < 3:
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise BackendUnavailable(f"{status} {exc.response.reason_phrase}") from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < 3:
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise BackendUnavailable(str(exc)) from exc
            except Exception as exc:
                raise BackendUnavailable(str(exc)) from exc
        raise BackendUnavailable("S3 request failed")

    def _signing_key(self, date_stamp: str) -> bytes:
        key = ("AWS4" + self.credentials.secret_key).encode()
        date_key = hmac.new(key, date_stamp.encode(), hashlib.sha256).digest()
        region_key = hmac.new(date_key, self.region.encode(), hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()

    def _canonical_query(self, query: dict[str, str]) -> str:
        return "&".join(
            f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(value, safe='-_.~')}"
            for key, value in sorted(query.items())
        )
