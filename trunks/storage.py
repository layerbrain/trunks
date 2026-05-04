from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


SECRET_KEYS = {
    "access_key",
    "access_key_id",
    "account_key",
    "application_key",
    "password",
    "passphrase",
    "sas_token",
    "secret_access_key",
    "secret_key",
    "session_token",
}


@dataclass(frozen=True)
class Storage:
    name: str
    backend: str
    role: str
    settings: dict[str, str] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_url(cls, *, name: str, role: str, url: str) -> "Storage":
        return cls(name=name, backend="url", role=role, settings={"url": url})

    @classmethod
    def from_record(cls, raw: dict[str, Any]) -> "Storage":
        return cls(
            name=str(raw["name"]),
            backend=str(raw["backend"]),
            role=str(raw["role"]),
            settings={str(k): str(v) for k, v in dict(raw.get("settings", {})).items() if v is not None},
            credentials={str(k): str(v) for k, v in dict(raw.get("credentials", {})).items() if v is not None},
        )

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "backend": self.backend,
            "role": self.role,
            "settings": self.settings,
            "credentials": self.credentials,
        }

    def public_record(self, repo_name: str) -> dict[str, object]:
        record: dict[str, object] = {
            "name": self.name,
            "role": self.role,
            "url": self.url(repo_name),
        }
        if self.backend != "url":
            record["backend"] = self.backend
            record["settings"] = self._public_settings()
        return record

    def masked_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "backend": self.backend,
            "role": self.role,
            "settings": self._masked(self.settings),
            "credentials": self._masked(self.credentials),
        }

    def with_env_overrides(self) -> "Storage":
        prefix = f"TRUNKS_STORAGE_{self.name.upper().replace('-', '_').replace('.', '_')}_"
        settings = dict(self.settings)
        credentials = dict(self.credentials)
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            target = key[len(prefix) :].lower()
            if target in SECRET_KEYS or target.endswith("_key") or target.endswith("_token"):
                credentials[target] = value
            else:
                settings[target] = value
        return Storage(self.name, self.backend, self.role, settings, credentials)

    def url(self, repo_name: str) -> str:
        if self.backend == "url":
            return self._format_repo(self.settings["url"], repo_name)
        if self.backend in {"s3", "r2", "tigris", "b2", "wasabi", "spaces", "ceph", "netapp", "gcs-s3"}:
            return self._bucket_url(self.backend, repo_name)
        if self.backend == "minio":
            host = self._required("host")
            bucket = self._required("bucket")
            prefix = self._prefix(repo_name)
            return f"minio://{host}/{bucket}/{prefix}".rstrip("/")
        if self.backend in {"local", "file", "nfs", "smb", "nas"}:
            path = self._required("path").rstrip("/")
            if path.endswith(".trunk"):
                return f"{self.backend}://{path}"
            return f"{self.backend}://{path}/trunks/{repo_name}.trunk"
        if self.backend == "sftp":
            user = self.settings.get("user")
            host = self._required("host")
            port = f":{self.settings['port']}" if self.settings.get("port") else ""
            auth_host = f"{user}@{host}" if user else host
            path = self._required("path").rstrip("/")
            if not path.endswith(".trunk"):
                path = f"{path}/trunks/{repo_name}.trunk"
            return f"sftp://{auth_host}{port}{path if path.startswith('/') else '/' + path}"
        if self.backend == "postgres":
            dsn = self.settings.get("dsn")
            if not dsn and self.settings.get("dsn_env"):
                dsn = os.environ.get(self.settings["dsn_env"])
            if not dsn:
                raise ValueError(f"storage {self.name!r} missing required field 'dsn'")
            dsn = dsn.rstrip("/")
            trunk = self.settings.get("trunk") or f"trunks/{repo_name}.trunk"
            return f"{dsn}/{trunk.strip('/')}"
        if self.backend == "azure":
            account = self._required("account_name")
            container = self._required("container")
            return f"azure://{account}/{container}/{self._prefix(repo_name)}"
        if self.backend == "gcs":
            return self._bucket_url("gcs", repo_name)
        raise ValueError(f"unsupported storage backend: {self.backend}")

    def _bucket_url(self, scheme: str, repo_name: str) -> str:
        bucket = self._required("bucket")
        prefix = self._prefix(repo_name)
        return f"{scheme}://{bucket}/{prefix}".rstrip("/")

    def _prefix(self, repo_name: str) -> str:
        return self._format_repo(self.settings.get("prefix") or "trunks/{repo}.trunk", repo_name).strip("/")

    def _format_repo(self, value: str, repo_name: str) -> str:
        return value.replace("{repo}", repo_name)

    def _required(self, key: str) -> str:
        value = self.settings.get(key) or self.credentials.get(key)
        if not value:
            raise ValueError(f"storage {self.name!r} missing required field {key!r}")
        return value

    def _masked(self, data: dict[str, str]) -> dict[str, str]:
        result = {}
        for key, value in data.items():
            if key in SECRET_KEYS or key.endswith("_key") or key.endswith("_token"):
                result[key] = "****" if value else ""
            else:
                result[key] = value
        return result

    def _public_settings(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.settings.items()
            if key not in SECRET_KEYS
            and not key.endswith("_key")
            and not key.endswith("_token")
            and key not in {"password_env", "dsn_env"}
        }
