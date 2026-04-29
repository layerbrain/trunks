from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _speed
from .ids import ulid
from .repository import Repository


@dataclass(frozen=True)
class AuditEvent:
    id: str
    timestamp: float
    event: str
    data: dict[str, Any]

    def serialize(self) -> str:
        return _speed.dumps(
            {"id": self.id, "ts": self.timestamp, "event": self.event, "data": self.data},
        ).decode("utf-8")

    def to_record(self) -> dict[str, Any]:
        return {"id": self.id, "ts": self.timestamp, "event": self.event, "data": self.data}


def record(repo: Repository, event: str, data: dict[str, Any] | None = None) -> AuditEvent:
    entry = AuditEvent(id=ulid(), timestamp=time.time(), event=event, data=data or {})
    path = _path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(entry.serialize() + "\n")
    return entry


def read(repo: Repository, *, limit: int | None = None) -> list[AuditEvent]:
    path = _path(repo)
    if not path.exists():
        return []
    events: list[AuditEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = _speed.loads(line)
        events.append(
            AuditEvent(
                id=str(raw["id"]),
                timestamp=float(raw["ts"]),
                event=str(raw["event"]),
                data=dict(raw.get("data", {})),
            )
        )
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


def _path(repo: Repository) -> Path:
    return repo.path.parent / "audit.log"
