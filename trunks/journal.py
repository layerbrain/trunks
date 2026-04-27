from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .ids import ObjectId, TxId, ulid


@dataclass(frozen=True)
class JournalEntry:
    id: TxId
    kind: str
    data: dict[str, Any]
    created_at: datetime

    @classmethod
    def create(cls, kind: str, data: dict[str, Any], *, id: TxId | None = None) -> "JournalEntry":
        return cls(id or ulid(), kind, data, datetime.now(UTC))

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "kind": self.kind,
                "data": self.data,
                "created_at": self.created_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "JournalEntry":
        data = json.loads(raw)
        return cls(
            id=data["id"],
            kind=data["kind"],
            data=data["data"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )


def oid_list(values: list[ObjectId]) -> list[str]:
    return [str(value) for value in values]
