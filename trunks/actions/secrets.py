from __future__ import annotations

import json
import os
from collections.abc import Mapping

from trunks.sandboxes import LogChunk
from trunks.objects import Blob
from trunks.repository import Repository

from .backend_store import secret_prefix, secret_ref, store

SECRET_BINDING_SCHEMA = "trunks.actions.secret_binding.v1"
MASKED_SECRET = "***"


def bind_secret(repo: Repository, name: str, source: str) -> dict[str, object]:
    _validate_secret_name(name)
    binding = {
        "_schema_version": SECRET_BINDING_SCHEMA,
        "object": "secret_binding",
        "name": name,
        "source": source,
    }
    blob = Blob.from_data(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode())
    action_store = store(repo)
    action_store.write_blob(blob)
    action_store.set_ref(secret_ref(repo, name), blob.id)
    return binding


def list_secret_bindings(repo: Repository) -> list[dict[str, object]]:
    action_store = store(repo)
    refs = action_store.list_refs(secret_prefix(repo))
    refs.sort(key=lambda item: item[0])
    return [json.loads(action_store.read_blob_payload(oid).decode()) for _name, oid in refs]


def show_secret_binding(repo: Repository, name: str) -> dict[str, object]:
    action_store = store(repo)
    oid = action_store.read_ref(secret_ref(repo, name))
    if oid is None:
        raise KeyError(f"unknown action secret binding {name!r}")
    return json.loads(action_store.read_blob_payload(oid).decode())


def remove_secret_binding(repo: Repository, name: str) -> dict[str, object]:
    binding = show_secret_binding(repo, name)
    store(repo).delete_ref(secret_ref(repo, name))
    return {**binding, "deleted": True}


def resolve_secret_env(repo: Repository) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for binding in list_secret_bindings(repo):
        name = binding.get("name")
        source = binding.get("source")
        if not isinstance(name, str) or not isinstance(source, str):
            continue
        if source.startswith("env:"):
            env_name = source.removeprefix("env:")
            if env_name in os.environ:
                resolved[name] = os.environ[env_name]
    return resolved


def mask_secret_text(text: str, env: Mapping[str, str]) -> str:
    masked = text
    for value in sorted(_maskable_values(env), key=len, reverse=True):
        masked = masked.replace(value, MASKED_SECRET)
    return masked


def mask_log_chunk(log: LogChunk, env: Mapping[str, str]) -> LogChunk:
    return LogChunk(stream=log.stream, seq=log.seq, text=mask_secret_text(log.text, env), ts_ms=log.ts_ms)


def masking_env(secret_env: Mapping[str, str], extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    masked = dict(secret_env)
    for name, value in (extra_env or {}).items():
        if _sensitive_env_name(name):
            masked[name] = value
    return masked


def _validate_secret_name(name: str) -> None:
    if not name or any(not (char.isalnum() or char == "_") for char in name):
        raise ValueError("secret name must contain only letters, numbers, and underscores")


def _maskable_values(env: Mapping[str, str]) -> set[str]:
    return {value for value in env.values() if value and not value.isspace()}


def _sensitive_env_name(name: str) -> bool:
    parts = name.upper().replace("-", "_").split("_")
    return any(part in {"SECRET", "TOKEN", "PASSWORD", "PASS", "KEY", "CREDENTIAL", "CREDENTIALS"} for part in parts)
