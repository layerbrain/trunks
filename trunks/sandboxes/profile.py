from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any


SECRET_KEYS = {
    "api_key",
    "access_key",
    "access_token",
    "auth_token",
    "client_secret",
    "key",
    "password",
    "secret",
    "secret_key",
    "session_token",
    "token",
}


_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


@dataclass(frozen=True)
class SandboxProviderProfile:
    name: str
    type: str
    priority: int = 100
    enabled: bool = True
    settings: dict[str, str] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.match(self.name):
            raise ValueError(
                f"invalid sandbox provider name {self.name!r}: "
                "must be lowercase alphanumeric with optional hyphens or underscores"
            )
        if not self.type:
            raise ValueError("sandbox provider type is required")
        if self.priority < 0:
            raise ValueError("sandbox provider priority must be >= 0")

    @classmethod
    def from_record(cls, raw: dict[str, Any]) -> "SandboxProviderProfile":
        return cls(
            name=str(raw["name"]),
            type=str(raw["type"]),
            priority=int(raw.get("priority", 100)),
            enabled=bool(raw.get("enabled", True)),
            settings={
                str(k): str(v)
                for k, v in dict(raw.get("settings", {})).items()
                if v is not None
            },
            credentials={
                str(k): str(v)
                for k, v in dict(raw.get("credentials", {})).items()
                if v is not None
            },
        )

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.type,
            "priority": self.priority,
            "enabled": self.enabled,
            "settings": self.settings,
            "credentials": self.credentials,
        }

    def masked_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.type,
            "priority": self.priority,
            "enabled": self.enabled,
            "settings": self._masked(self.settings),
            "credentials": self._masked(self.credentials),
        }

    def with_env_overrides(self) -> "SandboxProviderProfile":
        prefix = f"TRUNKS_SANDBOX_{self.name.upper().replace('-', '_').replace('.', '_')}_"
        settings = dict(self.settings)
        credentials = dict(self.credentials)
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            target = key[len(prefix) :].lower()
            if _is_secret(target):
                credentials[target] = value
            else:
                settings[target] = value
        return SandboxProviderProfile(
            name=self.name,
            type=self.type,
            priority=self.priority,
            enabled=self.enabled,
            settings=settings,
            credentials=credentials,
        )

    def provider_config(self) -> dict[str, object]:
        merged: dict[str, object] = {}
        merged.update(self.settings)
        merged.update(self.credentials)
        return merged

    @staticmethod
    def _masked(data: dict[str, str]) -> dict[str, str]:
        result = {}
        for key, value in data.items():
            if _is_secret(key):
                result[key] = "****" if value else ""
            else:
                result[key] = value
        return result


def _is_secret(key: str) -> bool:
    if key in SECRET_KEYS:
        return True
    if key.endswith("_key") or key.endswith("_token") or key.endswith("_secret"):
        return True
    return False


def valid_sandbox_provider_name(value: str) -> bool:
    return bool(_NAME_PATTERN.match(value))
