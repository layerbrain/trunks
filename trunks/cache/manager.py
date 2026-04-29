from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ids import ObjectId
from ..refs import normalize_ref


DEFAULT_REF_TTL_SECONDS = 60
DEFAULT_NEGATIVE_TTL_SECONDS = 60


@dataclass(frozen=True)
class CacheStats:
    hits: int
    misses: int
    puts: int
    negative_hits: int
    objects: int
    size_bytes: int

    def to_json(self) -> dict[str, int | float]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "puts": self.puts,
            "negative_hits": self.negative_hits,
            "objects": self.objects,
            "size_bytes": self.size_bytes,
            "hit_ratio": self.hits / total if total else 0.0,
        }


class CacheManager:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        ref_ttl_seconds: int = DEFAULT_REF_TTL_SECONDS,
        negative_ttl_seconds: int = DEFAULT_NEGATIVE_TTL_SECONDS,
    ) -> None:
        self.root = Path(root or self.default_root()).expanduser()
        self.ref_ttl_seconds = ref_ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds

    @classmethod
    def default_root(cls) -> Path:
        if os.environ.get("TRUNKS_CACHE_DIR"):
            return Path(os.environ["TRUNKS_CACHE_DIR"])
        if os.environ.get("XDG_CACHE_HOME"):
            return Path(os.environ["XDG_CACHE_HOME"]) / "trunks"
        return Path.home() / ".cache" / "trunks"

    def get_object(self, oid: ObjectId) -> bytes | None:
        path = self._object_path(oid)
        if not path.exists():
            self._increment("misses")
            return None
        data = path.read_bytes()
        if ObjectId.from_bytes(data) != oid:
            path.unlink(missing_ok=True)
            self._increment("misses")
            return None
        self._touch(path)
        self._increment("hits")
        return data

    def put_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        path = self._object_path(oid)
        if path.exists():
            return
        self._write_atomic(path, data)
        self.clear_negative(oid)
        self._increment("puts")

    def has_negative(self, oid: ObjectId) -> bool:
        path = self._negative_path(oid)
        if not path.exists():
            return False
        if time.time() - path.stat().st_mtime > self.negative_ttl_seconds:
            path.unlink(missing_ok=True)
            return False
        self._increment("negative_hits")
        return True

    def put_negative(self, oid: ObjectId) -> None:
        self._write_atomic(self._negative_path(oid), b"")

    def clear_negative(self, oid: ObjectId) -> None:
        self._negative_path(oid).unlink(missing_ok=True)

    def get_ref(self, name: str) -> ObjectId | None | object:
        path = self._ref_path(name)
        if not path.exists():
            return _REF_MISS
        if time.time() - path.stat().st_mtime > self.ref_ttl_seconds:
            path.unlink(missing_ok=True)
            return _REF_MISS
        raw = json.loads(path.read_text(encoding="utf-8"))
        value = raw.get("oid")
        return ObjectId(value) if isinstance(value, str) else None

    def put_ref(self, name: str, oid: ObjectId | None) -> None:
        self._write_atomic(self._ref_path(name), json.dumps({"oid": str(oid) if oid else None}, sort_keys=True).encode())

    def clear_refs(self) -> None:
        self._remove_children(self.root / "refs")

    def clear(self) -> None:
        self._remove_children(self.root)

    def prune(self, *, max_size_bytes: int) -> int:
        files = sorted(self._object_files(), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in files)
        removed = 0
        while total > max_size_bytes and files:
            path = files.pop(0)
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    def verify(self) -> list[str]:
        errors: list[str] = []
        for path in self._object_files():
            try:
                oid = ObjectId(path.name)
                data = path.read_bytes()
                if ObjectId.from_bytes(data) != oid:
                    errors.append(f"corrupt object cache entry: {path}")
            except Exception as exc:
                errors.append(f"invalid object cache entry: {path} ({exc})")
        return errors

    def stats(self) -> CacheStats:
        counters = self._read_counters()
        object_files = self._object_files()
        return CacheStats(
            hits=int(counters.get("hits", 0)),
            misses=int(counters.get("misses", 0)),
            puts=int(counters.get("puts", 0)),
            negative_hits=int(counters.get("negative_hits", 0)),
            objects=len(object_files),
            size_bytes=sum(path.stat().st_size for path in object_files),
        )

    def _object_path(self, oid: ObjectId) -> Path:
        return self.root / "objects" / oid.value[:2] / oid.value

    def _object_files(self) -> list[Path]:
        candidates: list[Path] = []
        direct = self.root / "objects"
        if direct.exists():
            candidates.append(direct)
        if self.root.exists():
            candidates.extend(path for path in self.root.rglob("objects") if path.is_dir() and path != direct)
        files: list[Path] = []
        seen: set[Path] = set()
        for directory in candidates:
            for path in directory.rglob("*"):
                if path.is_file() and path not in seen:
                    files.append(path)
                    seen.add(path)
        return files

    def _negative_path(self, oid: ObjectId) -> Path:
        return self.root / "negative" / oid.value[:2] / oid.value

    def _ref_path(self, name: str) -> Path:
        safe = normalize_ref(name).replace("/", "__")
        return self.root / "refs" / f"{safe}.json"

    def _stats_path(self) -> Path:
        return self.root / "stats.json"

    def _read_counters(self) -> dict[str, Any]:
        paths = [self._stats_path()] if self._stats_path().exists() else []
        if self.root.exists():
            paths.extend(path for path in self.root.rglob("stats.json") if path != self._stats_path())
        counters: dict[str, int] = {}
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for key, value in raw.items():
                counters[str(key)] = counters.get(str(key), 0) + int(value)
        return counters

    def _increment(self, key: str) -> None:
        counters = self._read_counters()
        counters[key] = int(counters.get(key, 0)) + 1
        self._write_atomic(self._stats_path(), json.dumps(counters, sort_keys=True).encode())

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _remove_children(self, path: Path) -> None:
        if not path.exists():
            return
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                child.rmdir()
        if path != self.root:
            path.rmdir()

    def _touch(self, path: Path) -> None:
        now = time.time()
        os.utime(path, (now, now))


_REF_MISS = object()
