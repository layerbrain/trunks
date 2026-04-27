from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import BackendUnavailable


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise BackendUnavailable(f"missing environment variable {name}")
    return value


@dataclass(frozen=True)
class S3Credentials:
    access_key: str
    secret_key: str
    session_token: str | None = None

    @classmethod
    def from_env(cls, scheme: str = "s3") -> "S3Credentials":
        try:
            if scheme == "s3":
                return cls(
                    access_key=_env("AWS_ACCESS_KEY_ID"),
                    secret_key=_env("AWS_SECRET_ACCESS_KEY"),
                    session_token=os.environ.get("AWS_SESSION_TOKEN"),
                )
            if scheme == "minio":
                return cls(access_key=_env("MINIO_ROOT_USER"), secret_key=_env("MINIO_ROOT_PASSWORD"))
            prefix = scheme.upper().replace("-", "_")
            key_name = f"{prefix}_ACCESS_KEY_ID"
            secret_name = f"{prefix}_SECRET_ACCESS_KEY"
            if scheme == "b2":
                key_name = "B2_KEY_ID"
                secret_name = "B2_APPLICATION_KEY"
            return cls(access_key=_env(key_name), secret_key=_env(secret_name))
        except BackendUnavailable as exc:
            raise BackendUnavailable(_credential_help(scheme)) from exc

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "S3Credentials":
        return cls(
            access_key=data.get("access_key") or data.get("access_key_id") or data.get("key_id") or "",
            secret_key=data.get("secret_key") or data.get("secret_access_key") or data.get("application_key") or "",
            session_token=data.get("session_token") or None,
        )


def _credential_help(scheme: str) -> str:
    if scheme == "minio":
        env = "MINIO_ROOT_USER and MINIO_ROOT_PASSWORD"
    elif scheme == "b2":
        env = "B2_KEY_ID and B2_APPLICATION_KEY"
    elif scheme == "s3":
        env = "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
    else:
        prefix = scheme.upper().replace("-", "_")
        env = f"{prefix}_ACCESS_KEY_ID and {prefix}_SECRET_ACCESS_KEY"
    return (
        f"missing credentials for {scheme}. Set {env}, or save local credentials with "
        f"`trunks storage add --name primary --backend {scheme} --bucket <bucket> "
        "--access-key <key> --secret-key <secret>`."
    )


@dataclass(frozen=True)
class AzureCredentials:
    account_name: str
    account_key: str | None = None
    sas_token: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "AzureCredentials":
        return cls(
            account_name=data.get("account_name", ""),
            account_key=data.get("account_key") or None,
            sas_token=data.get("sas_token") or None,
        )


@dataclass(frozen=True)
class GCSCredentials:
    service_account: dict | Path

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "GCSCredentials | None":
        path = data.get("service_account_file") or data.get("service_account")
        return cls(Path(path)) if path else None


@dataclass(frozen=True)
class SFTPCredentials:
    ssh_key: Path | None = None
    passphrase: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls) -> "SFTPCredentials | None":
        password = os.environ.get("SFTP_PASSWORD")
        key = os.environ.get("SFTP_KEY")
        if not password and not key:
            return None
        return cls(
            ssh_key=Path(key) if key else None,
            passphrase=os.environ.get("SFTP_PASSPHRASE"),
            password=password,
        )

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "SFTPCredentials | None":
        key = data.get("ssh_key")
        password = data.get("password")
        password_env = data.get("password_env")
        if password_env:
            password = os.environ.get(password_env)
        if not key and not password:
            return None
        return cls(
            ssh_key=Path(key) if key else None,
            passphrase=data.get("passphrase") or data.get("key_passphrase"),
            password=password,
        )


@dataclass(frozen=True)
class PostgresCredentials:
    dsn: str

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "PostgresCredentials":
        return cls(data["dsn"])
