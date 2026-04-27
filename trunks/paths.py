from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from pathlib import Path

from .errors import InvalidPath


def normalize_path(path: str) -> str:
    raw = path.replace("\\", "/")
    if raw in {"", ".", "/"}:
        raise InvalidPath(path)
    if any(ord(c) < 0x20 or c == "\x7f" for c in raw):
        raise InvalidPath(path)
    if raw.startswith("/") or raw.endswith("/"):
        raise InvalidPath(path)
    parts = PurePosixPath(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidPath(path)
    return "/".join(parts)


def is_internal_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    return any(part in {".git", ".trunks"} for part in normalized.split("/"))


DEFAULT_IGNORES = (
    ".git/",
    ".trunks/",
    "node_modules/",
    "__pycache__/",
    "build/",
    "dist/",
    ".DS_Store",
    "Thumbs.db",
)


def is_ignored(path: str, root: Path | None = None) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    if is_internal_path(normalized):
        return True
    ignored = False
    for pattern in DEFAULT_IGNORES:
        match = _matches_ignore(normalized, pattern)
        if match is not None:
            ignored = match
    if root is not None:
        for pattern in _ignore_patterns(root):
            match = _matches_ignore(normalized, pattern)
            if match is not None:
                ignored = match
    return ignored


def _ignore_patterns(root: Path) -> list[str]:
    patterns: list[str] = []
    for name in (".gitignore", ".trunksignore"):
        path = root / name
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def _matches_ignore(path: str, pattern: str) -> bool | None:
    negate = pattern.startswith("!") and not pattern.startswith("\\!")
    if negate:
        pattern = pattern[1:]
    pattern = pattern.strip().replace("\\!", "!").replace("\\#", "#").replace("\\", "/")
    if not pattern:
        return None
    if pattern.startswith("/"):
        pattern = pattern[1:]
    directory_only = pattern.endswith("/")
    if directory_only:
        pattern = pattern.rstrip("/")
    if not pattern:
        return None

    if directory_only:
        matched = _path_or_parent_matches(path, pattern)
    elif "/" in pattern:
        matched = _path_glob_matches(path, pattern)
    else:
        matched = any(fnmatch.fnmatchcase(part, pattern) for part in path.split("/"))
    if not matched:
        return None
    return not negate


def _path_or_parent_matches(path: str, pattern: str) -> bool:
    parts = path.split("/")
    if "/" not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    return any(
        _path_glob_matches("/".join(parts[:index]), pattern)
        for index in range(1, len(parts) + 1)
    )


def _path_glob_matches(path: str, pattern: str) -> bool:
    return _glob_parts_match(pattern.split("/"), path.split("/"))


def _glob_parts_match(pattern_parts: list[str], path_parts: list[str]) -> bool:
    if not pattern_parts:
        return not path_parts
    head = pattern_parts[0]
    if head == "**":
        return any(_glob_parts_match(pattern_parts[1:], path_parts[index:]) for index in range(len(path_parts) + 1))
    if not path_parts:
        return False
    if not fnmatch.fnmatchcase(path_parts[0], head):
        return False
    return _glob_parts_match(pattern_parts[1:], path_parts[1:])
