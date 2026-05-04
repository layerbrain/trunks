from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import Storage

if TYPE_CHECKING:
    from .sandboxes.profile import SandboxProviderProfile


CONFIG_DIR = Path.home() / ".trunks"
CONFIG_PATH = CONFIG_DIR / "config"


def config_dir() -> Path:
    return Path(os.environ.get("TRUNKS_HOME", str(CONFIG_DIR))).expanduser()


def config_path() -> Path:
    return config_dir() / "config"


@dataclass(frozen=True)
class GlobalConfig:
    default_backend: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "GlobalConfig":
        parser = _load_parser(path)
        return cls(default_backend=parser.get("default", "backend", fallback=None))

    def save(self, path: Path | None = None) -> None:
        parser = _load_parser(path)
        if "default" not in parser:
            parser["default"] = {}
        if self.default_backend:
            parser["default"]["backend"] = self.default_backend
        else:
            parser["default"].pop("backend", None)
        _write_parser(parser, path)


def load_sandbox_provider_profiles(path: Path | None = None) -> list[SandboxProviderProfile]:
    parser = _load_parser(path)
    profiles = []
    for section in sorted(section for section in parser.sections() if section.startswith("sandbox_provider.")):
        name = section.removeprefix("sandbox_provider.")
        profile = _sandbox_provider_profile_from_section(name, parser[section])
        profiles.append(profile)
    return sorted(profiles, key=lambda item: (item.priority, item.name))


def get_sandbox_provider_profile(name: str, path: Path | None = None) -> SandboxProviderProfile | None:
    parser = _load_parser(path)
    section = f"sandbox_provider.{name}"
    if not parser.has_section(section):
        return None
    return _sandbox_provider_profile_from_section(name, parser[section])


def set_sandbox_provider_profile(
    profile: SandboxProviderProfile,
    path: Path | None = None,
) -> None:
    parser = _load_parser(path)
    section = f"sandbox_provider.{profile.name}"
    parser[section] = {
        "type": profile.type,
        "priority": str(profile.priority),
        "enabled": "true" if profile.enabled else "false",
    }
    for key, value in sorted(profile.settings.items()):
        parser[section][f"setting.{key}"] = value
    for key, value in sorted(profile.credentials.items()):
        parser[section][f"credential.{key}"] = value
    _write_parser(parser, path)


def remove_sandbox_provider_profile(name: str, path: Path | None = None) -> bool:
    parser = _load_parser(path)
    removed = parser.remove_section(f"sandbox_provider.{name}")
    if removed:
        _write_parser(parser, path)
    return removed


def load_storage_profiles(path: Path | None = None) -> list[Storage]:
    parser = _load_parser(path)
    profiles = []
    for section in sorted(section for section in parser.sections() if section.startswith("storage.")):
        name = section.removeprefix("storage.")
        profiles.append(_storage_profile_from_section(name, parser[section]))
    return sorted(profiles, key=lambda item: (item.role != "primary", item.name))


def get_storage_profile(name: str, path: Path | None = None) -> Storage | None:
    parser = _load_parser(path)
    section = f"storage.{name}"
    if not parser.has_section(section):
        return None
    return _storage_profile_from_section(name, parser[section])


def set_storage_profile(storage: Storage, path: Path | None = None) -> None:
    parser = _load_parser(path)
    section = f"storage.{storage.name}"
    parser[section] = {
        "backend": storage.backend,
        "role": storage.role,
    }
    for key, value in sorted(storage.settings.items()):
        parser[section][f"setting.{key}"] = value
    for key, value in sorted(storage.credentials.items()):
        parser[section][f"credential.{key}"] = value
    _write_parser(parser, path)


def remove_storage_profile(name: str, path: Path | None = None) -> bool:
    parser = _load_parser(path)
    removed = parser.remove_section(f"storage.{name}")
    if removed:
        _write_parser(parser, path)
    return removed


def _load_parser(path: Path | None) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    target = path or config_path()
    if target.exists():
        parser.read(target)
    return parser


def _write_parser(parser: configparser.ConfigParser, path: Path | None) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    target.chmod(0o600)


def _sandbox_provider_profile_from_section(
    name: str,
    section: configparser.SectionProxy,
) -> SandboxProviderProfile:
    from .sandboxes.profile import SandboxProviderProfile

    return SandboxProviderProfile(
        name=name,
        type=section["type"],
        priority=int(section.get("priority", "100")),
        enabled=_as_bool(section.get("enabled", "true")),
        settings=_prefixed_values(section, "setting."),
        credentials=_prefixed_values(section, "credential."),
    )


def _storage_profile_from_section(name: str, section: configparser.SectionProxy) -> Storage:
    return Storage(
        name=name,
        backend=section["backend"],
        role=section.get("role", "primary"),
        settings=_prefixed_values(section, "setting."),
        credentials=_prefixed_values(section, "credential."),
    )


def _prefixed_values(section: configparser.SectionProxy, prefix: str) -> dict[str, str]:
    return {
        key.removeprefix(prefix): value
        for key, value in section.items()
        if key.startswith(prefix)
    }


def _as_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RepoConfig:
    name: str
    backend: str | None = None
    push_mode: str = "trunks-only"


def resolve_backend_url(base: str | None, repo_name: str) -> str | None:
    if not base:
        return None
    if "{repo}" in base:
        return base.format(repo=repo_name)
    scheme = base.split("://", 1)[0] if "://" in base else ""
    if scheme == "postgres":
        from urllib.parse import urlsplit
        parts = urlsplit(base)
        path = parts.path.lstrip("/")
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 2:
            return base
        if len(segments) == 1:
            return f"{base.rstrip('/')}/{repo_name}"
        return None
    if scheme == "sqlite":
        if "#" in base:
            return base
        return f"{base.rstrip('/')}#{repo_name}"
    if base.rstrip("/").endswith(".trunk"):
        return base
    return f"{base.rstrip('/')}/trunks/{repo_name}.trunk"


def repo_name_from_env(default: str) -> str:
    return os.environ.get("TRUNKS_REPO_NAME") or default


def env_forces_local_only() -> bool:
    return os.environ.get("TRUNKS_LOCAL_ONLY", "").lower() in {"1", "true", "yes", "on"}


def backend_from_env(repo_name: str) -> str | None:
    if env_forces_local_only():
        return None
    if repo := os.environ.get("TRUNKS_REPO"):
        return repo
    if base := os.environ.get("TRUNKS_BACKEND"):
        return resolve_backend_url(base, repo_name)
    return None


def mirror_backends_from_env(repo_name: str) -> list[str]:
    raw = os.environ.get("TRUNKS_MIRRORS", "")
    return [
        resolved
        for item in (part.strip() for part in raw.split(","))
        if item
        for resolved in [resolve_backend_url(item, repo_name)]
        if resolved
    ]


def push_mode_from_env(default: str) -> str:
    value = os.environ.get("TRUNKS_PUSH_MODE")
    if value in {"mirror", "trunks-only", "manual"}:
        return value
    return default


def mirror_policy_from_env(default: str) -> str:
    value = os.environ.get("TRUNKS_MIRROR_POLICY")
    if value in {"strict", "best-effort"}:
        return value
    return default
