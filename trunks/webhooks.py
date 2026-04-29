from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .ids import ulid
from .journal import JournalEntry
from .repository import Repository


@dataclass(frozen=True)
class Webhook:
    id: str
    url: str
    events: tuple[str, ...]
    secret: str

    def public_record(self) -> dict[str, object]:
        return {"id": self.id, "url": self.url, "events": list(self.events)}

    def record(self) -> dict[str, object]:
        return {"id": self.id, "url": self.url, "events": list(self.events), "secret": self.secret}


def add(repo: Repository, url: str, events: list[str]) -> Webhook:
    hook = Webhook(id=ulid(), url=url, events=tuple(events), secret=secrets.token_hex(32))
    hooks = _read(repo)
    hooks.append(hook)
    _write(repo, hooks)
    return hook


def list_hooks(repo: Repository) -> list[Webhook]:
    return _read(repo)


def get(repo: Repository, hook_id: str) -> Webhook | None:
    return next((hook for hook in _read(repo) if hook.id == hook_id), None)


def update(repo: Repository, hook_id: str, *, url: str | None = None, events: list[str] | None = None) -> Webhook | None:
    hooks = _read(repo)
    updated: Webhook | None = None
    next_hooks: list[Webhook] = []
    for hook in hooks:
        if hook.id == hook_id:
            updated = Webhook(
                id=hook.id,
                url=url or hook.url,
                events=tuple(events if events is not None else hook.events),
                secret=hook.secret,
            )
            next_hooks.append(updated)
        else:
            next_hooks.append(hook)
    if updated is None:
        return None
    _write(repo, next_hooks)
    return updated


def delete(repo: Repository, hook_id: str) -> bool:
    hooks = _read(repo)
    kept = [hook for hook in hooks if hook.id != hook_id]
    if len(kept) == len(hooks):
        return False
    _write(repo, kept)
    return True


async def emit(repo: Repository, event: str, payload: dict[str, Any]) -> None:
    hooks = [hook for hook in _read(repo) if event in hook.events]
    if not hooks:
        return
    body = json.dumps({"event": event, "repository": repo.name, **payload}, sort_keys=True).encode()
    await asyncio.gather(*[_deliver(repo, hook, event, body) for hook in hooks])


async def _deliver(repo: Repository, hook: Webhook, event: str, body: bytes) -> None:
    from . import audit

    signature = hmac.new(hook.secret.encode(), body, sha256).hexdigest()
    request = urllib.request.Request(
        hook.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Trunks-Event": event,
            "X-Trunks-Signature": f"sha256={signature}",
        },
    )
    try:
        await asyncio.to_thread(_open_and_close, request)
    except Exception as exc:
        repo.append_journal(
            JournalEntry.create(
                "webhook_failed",
                {"webhook": hook.id, "url": hook.url, "error": str(exc)},
            )
        )
        audit.record(
            repo,
            "webhook.failed",
            {"webhook": hook.id, "url": hook.url, "event": event, "error": str(exc)},
        )
        return
    audit.record(
        repo,
        "webhook.delivered",
        {"webhook": hook.id, "url": hook.url, "event": event},
    )


def _open_and_close(request: urllib.request.Request) -> None:
    with urllib.request.urlopen(request, timeout=5):
        pass


def _path(repo: Repository) -> Path:
    return repo.path.parent / "webhooks.json"


def _read(repo: Repository) -> list[Webhook]:
    path = _path(repo)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Webhook(
            id=str(item["id"]),
            url=str(item["url"]),
            events=tuple(str(event) for event in item.get("events", [])),
            secret=str(item["secret"]),
        )
        for item in raw
        if isinstance(item, dict)
    ]


def _write(repo: Repository, hooks: list[Webhook]) -> None:
    path = _path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps([hook.record() for hook in hooks], sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
