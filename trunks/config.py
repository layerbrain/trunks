from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path


CONFIG_DIR = Path.home() / ".trunks"
CONFIG_PATH = CONFIG_DIR / "config"


@dataclass(frozen=True)
class GlobalConfig:
    default_backend: str | None = None

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "GlobalConfig":
        parser = configparser.ConfigParser()
        if path.exists():
            parser.read(path)
        return cls(default_backend=parser.get("default", "backend", fallback=None))

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        parser = configparser.ConfigParser()
        parser["default"] = {}
        if self.default_backend:
            parser["default"]["backend"] = self.default_backend
        with path.open("w", encoding="utf-8") as handle:
            parser.write(handle)


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
