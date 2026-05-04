from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Backend
    from .storage import Storage


@dataclass(frozen=True)
class BackendURL:
    raw: str
    scheme: str
    bucket: str | None = None
    path: str = ""
    endpoint: str | None = None

    @classmethod
    def parse(cls, value: str | None) -> "BackendURL":
        if value is None or value == "":
            return cls("memory://", "memory")
        if value == "memory":
            return cls("memory://", "memory")
        if "://" not in value and value != "memory":
            return cls(value, "local", path=str(Path(value)))
        parsed = urlparse(value)
        scheme = parsed.scheme
        if scheme == "memory":
            return cls(value, scheme)
        if scheme in {"file", "sqlite", "local", "nfs", "smb", "nas"}:
            return cls(value, scheme, path=parsed.path)
        bucket = parsed.netloc
        path = parsed.path.lstrip("/")
        return cls(value, scheme, bucket=bucket, path=path)


@dataclass(frozen=True)
class S3SchemeDefaults:
    bucket: str
    prefix: str
    endpoint: str | None
    region: str | None
    credentials_scheme: str


@dataclass(frozen=True)
class BackendScheme:
    backend: str
    credentials_scheme: str | None = None


SCHEMES: dict[str, BackendScheme] = {
    "memory": BackendScheme("trunks.backends.memory:Memory"),
    "sqlite": BackendScheme("trunks.backends.sqlite:SQLite"),
    "file": BackendScheme("trunks.backends.fileshare:FileShare"),
    "nfs": BackendScheme("trunks.backends.fileshare:FileShare"),
    "smb": BackendScheme("trunks.backends.fileshare:FileShare"),
    "nas": BackendScheme("trunks.backends.fileshare:FileShare"),
    "local": BackendScheme("trunks.backends.local:Local"),
    "s3": BackendScheme("trunks.backends.s3:S3", "s3"),
    "r2": BackendScheme("trunks.backends.s3:S3", "r2"),
    "tigris": BackendScheme("trunks.backends.s3:S3", "tigris"),
    "minio": BackendScheme("trunks.backends.s3:S3", "minio"),
    "b2": BackendScheme("trunks.backends.s3:S3", "b2"),
    "wasabi": BackendScheme("trunks.backends.s3:S3", "wasabi"),
    "spaces": BackendScheme("trunks.backends.s3:S3", "spaces"),
    "ceph": BackendScheme("trunks.backends.s3:S3", "ceph"),
    "netapp": BackendScheme("trunks.backends.s3:S3", "netapp"),
    "gcs-s3": BackendScheme("trunks.backends.s3:S3", "gcs-s3"),
    "azure": BackendScheme("trunks.backends.azure:Azure", "azure"),
    "gcs": BackendScheme("trunks.backends.gcs:GCS", "gcs"),
    "sftp": BackendScheme("trunks.backends.sftp:SFTP", "sftp"),
    "postgres": BackendScheme("trunks.backends.postgres:Postgres", "postgres"),
}


def s3_scheme_defaults(value: str, *, endpoint: str | None, region: str | None) -> S3SchemeDefaults:
    parsed = urlparse(value)
    scheme = parsed.scheme
    bucket = parsed.netloc
    prefix = parsed.path.strip("/")
    credentials_scheme = SCHEMES[scheme].credentials_scheme or "s3"
    if scheme == "r2":
        import os

        account = os.environ.get("R2_ACCOUNT_ID") or bucket
        endpoint = endpoint or f"https://{account}.r2.cloudflarestorage.com"
        region = "auto"
    elif scheme == "tigris":
        endpoint = endpoint or "https://fly.storage.tigris.dev"
    elif scheme == "minio":
        endpoint = endpoint or f"http://{bucket}"
        parts = prefix.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
    return S3SchemeDefaults(bucket, prefix, endpoint, region, credentials_scheme)


def backend_from_url(value: str | "Backend" | None) -> "Backend | None":
    from .backend import Backend

    if value is None:
        return None
    if isinstance(value, Backend):
        return value
    parsed = BackendURL.parse(value)
    if parsed.scheme == "memory":
        from .backends.memory import Memory

        return Memory()
    if parsed.scheme == "sqlite":
        from .backends.sqlite import SQLite

        return SQLite.from_url(value)
    if parsed.scheme in {"file", "nfs", "smb", "nas"}:
        from .backends.fileshare import FileShare

        return FileShare(parsed.path)
    if parsed.scheme == "local":
        from .backends.local import Local

        return Local(parsed.path)
    if parsed.scheme in {"s3", "r2", "tigris", "minio", "b2", "wasabi", "spaces", "ceph", "netapp", "gcs-s3"}:
        from .backends.s3 import S3

        return S3.from_url(value)
    if parsed.scheme == "azure":
        from .backends.azure import Azure

        return Azure.from_url(value)
    if parsed.scheme == "gcs":
        from .backends.gcs import GCS

        return GCS.from_url(value)
    if parsed.scheme == "sftp":
        from .backends.sftp import SFTP

        return SFTP.from_url(value)
    if parsed.scheme == "postgres":
        from .backends.postgres import Postgres

        return Postgres.from_url(value)
    raise ValueError(f"unknown backend scheme: {parsed.scheme!r}")


def backend_from_storage(storage: "Storage", repo_name: str) -> "Backend | None":
    from .backend import Role
    from .config import get_storage_profile
    from .credentials import S3Credentials
    from .storage import Storage

    global_profile = get_storage_profile(storage.name)
    if global_profile is not None:
        storage = Storage(
            name=storage.name,
            backend=storage.backend,
            role=storage.role,
            settings={**global_profile.settings, **storage.settings},
            credentials={**global_profile.credentials, **storage.credentials},
        )
    profile = storage.with_env_overrides()
    role = Role.mirror if profile.role == "mirror" else Role.primary
    url = profile.url(repo_name)

    if profile.backend in {"url", "memory", "local", "file", "nfs", "smb", "nas", "sqlite"}:
        backend = backend_from_url(url)
        if backend is not None:
            backend.role = role
        return backend

    if profile.backend in {"s3", "r2", "tigris", "minio", "b2", "wasabi", "spaces", "ceph", "netapp", "gcs-s3"}:
        from .backends.s3 import S3

        endpoint = profile.settings.get("endpoint")
        if profile.backend == "r2" and endpoint is None and profile.settings.get("account_id"):
            endpoint = f"https://{profile.settings['account_id']}.r2.cloudflarestorage.com"
        region = profile.settings.get("region")
        defaults = s3_scheme_defaults(url, endpoint=endpoint, region=region)
        credentials = (
            S3Credentials.from_dict(profile.credentials)
            if profile.credentials
            else S3Credentials.from_env(defaults.credentials_scheme)
        )
        return S3(
            bucket=defaults.bucket,
            prefix=defaults.prefix,
            endpoint=defaults.endpoint,
            region=defaults.region,
            credentials=credentials,
            role=role,
        )

    if profile.backend == "azure":
        from .backends.azure import Azure
        from .credentials import AzureCredentials

        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/", 1)
        container = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        credential_data = {**profile.settings, **profile.credentials}
        return Azure(
            account=parsed.netloc,
            container=container,
            prefix=prefix,
            endpoint=profile.settings.get("endpoint"),
            credentials=AzureCredentials.from_dict(credential_data),
            role=role,
        )

    if profile.backend == "gcs":
        from .backends.gcs import GCS
        from .credentials import GCSCredentials

        parsed = urlparse(url)
        credential_data = {**profile.settings, **profile.credentials}
        return GCS(
            bucket=parsed.netloc,
            prefix=parsed.path.strip("/"),
            project=profile.settings.get("project"),
            endpoint=profile.settings.get("endpoint"),
            credentials=GCSCredentials.from_dict(credential_data),
            role=role,
        )

    if profile.backend == "sftp":
        from .backends.sftp import SFTP
        from .credentials import SFTPCredentials
        from urllib.parse import unquote

        parsed = urlparse(url)
        credentials = SFTPCredentials.from_dict(profile.credentials)
        return SFTP(
            host=parsed.hostname or "localhost",
            port=parsed.port or 22,
            username=unquote(parsed.username) if parsed.username else profile.settings.get("user"),
            credentials=credentials,
            root=parsed.path or "/trunks.trunk",
            role=role,
        )

    if profile.backend == "postgres":
        from .backends.postgres import Postgres

        backend = Postgres.from_url(url)
        backend.role = role
        return backend

    raise ValueError(f"unsupported storage backend: {profile.backend!r}")


def repo_name_from_origin(url: str) -> str:
    candidate = url.rstrip("/")
    if ":" in candidate and "://" not in candidate:
        candidate = candidate.rsplit(":", 1)[1]
    else:
        parsed = urlparse(candidate)
        candidate = parsed.path or candidate
    name = candidate.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if name.endswith(".trunk"):
        name = name[:-6]
    return name or "repo"
