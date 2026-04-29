"""Per-target runtime records for virtual mounts.

The supervisor process writes a small JSON record after the OS mount succeeds.
The CLI reads it on unmount to find the daemon pid and the repo data path.
Records live under `~/.trunks/active/<sha>.json` so a target path identifies
its own runtime entry deterministically.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


def _registry_dir() -> Path:
    return Path(os.environ.get("TRUNKS_HOME", str(Path.home() / ".trunks"))) / "active"


def _key_for(target: Path) -> str:
    resolved = str(target.expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def record_path(target: Path) -> Path:
    return _registry_dir() / f"{_key_for(target)}.json"


@dataclass(frozen=True)
class Record:
    target: Path
    repo_path: Path
    export: str
    host: str
    port: int
    pid: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "target": str(self.target),
                "repo_path": str(self.repo_path),
                "export": self.export,
                "host": self.host,
                "port": self.port,
                "pid": self.pid,
            }
        )

    @classmethod
    def from_json(cls, data: str) -> "Record":
        obj = json.loads(data)
        return cls(
            target=Path(obj["target"]),
            repo_path=Path(obj["repo_path"]),
            export=obj["export"],
            host=obj["host"],
            port=int(obj["port"]),
            pid=int(obj["pid"]),
        )


def write(record: Record) -> Path:
    path = record_path(record.target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.to_json(), encoding="utf-8")
    path.chmod(0o600)
    return path


def read(target: Path) -> Record | None:
    path = record_path(target)
    if not path.exists():
        return None
    return Record.from_json(path.read_text(encoding="utf-8"))


def find_for_path(path: Path) -> Record | None:
    target = path.expanduser().resolve()
    directory = _registry_dir()
    if not directory.exists():
        return None
    best: Record | None = None
    for record_file in directory.glob("*.json"):
        try:
            record = Record.from_json(record_file.read_text(encoding="utf-8"))
            record_target = record.target.expanduser().resolve()
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        if target == record_target or record_target in target.parents:
            if best is None or len(record_target.parts) > len(best.target.parts):
                best = record
    return best


def remove(target: Path) -> None:
    record_path(target).unlink(missing_ok=True)
