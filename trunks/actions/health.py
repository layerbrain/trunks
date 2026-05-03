from __future__ import annotations

import json
import time

from trunks.objects import Blob
from trunks.repository import Repository

from .backend_store import provider_health_prefix, provider_health_ref, store

HEALTH_SCHEMA = "trunks.actions.provider_health.v1"
DEGRADED_SECONDS = 60


def record_provider_success(
    repo: Repository,
    *,
    provider: str,
    region: str,
    spec_key: str,
    run: str,
    now_s: int | None = None,
) -> dict[str, object]:
    return _write_health(
        repo,
        {
            "_schema_version": HEALTH_SCHEMA,
            "object": "provider_health",
            "provider": provider,
            "region": region,
            "spec_key": spec_key,
            "run": run,
            "status": "healthy",
            "reason": None,
            "failures": 0,
            "ts_s": _now(now_s),
            "healthy_after_s": None,
        },
    )


def record_provider_failure(
    repo: Repository,
    *,
    provider: str,
    region: str,
    spec_key: str,
    run: str,
    reason: str,
    now_s: int | None = None,
) -> dict[str, object]:
    now = _now(now_s)
    previous = _read_health(repo, provider=provider, region=region, spec_key=spec_key)
    failures = 1
    if previous and previous.get("status") == "degraded" and isinstance(previous.get("failures"), int):
        failures = int(previous["failures"]) + 1
    return _write_health(
        repo,
        {
            "_schema_version": HEALTH_SCHEMA,
            "object": "provider_health",
            "provider": provider,
            "region": region,
            "spec_key": spec_key,
            "run": run,
            "status": "degraded",
            "reason": reason,
            "failures": failures,
            "ts_s": now,
            "healthy_after_s": now + DEGRADED_SECONDS,
        },
    )


def degraded_provider_ids(
    repo: Repository,
    *,
    region: str | None,
    spec_key: str,
    now_s: int | None = None,
) -> frozenset[str]:
    now = _now(now_s)
    degraded: set[str] = set()
    action_store = store(repo)
    for _name, oid in action_store.list_refs(provider_health_prefix()):
        try:
            payload = json.loads(action_store.read_blob_payload(oid).decode())
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("status") != "degraded" or payload.get("spec_key") != spec_key:
            continue
        if region is not None and payload.get("region") != region:
            continue
        healthy_after = payload.get("healthy_after_s")
        if isinstance(healthy_after, int) and healthy_after > now and isinstance(payload.get("provider"), str):
            degraded.add(str(payload["provider"]))
    return frozenset(degraded)


def provider_health_snapshot(repo: Repository) -> dict[str, object]:
    action_store = store(repo)
    data: list[dict[str, object]] = []
    for _name, oid in action_store.list_refs(provider_health_prefix()):
        try:
            payload = json.loads(action_store.read_blob_payload(oid).decode())
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            data.append(payload)
    data.sort(key=lambda item: (str(item.get("provider")), str(item.get("region")), str(item.get("spec_key"))))
    return {
        "_schema_version": "trunks.actions.provider_health_snapshot.v1",
        "object": "provider_health_snapshot",
        "data": data,
    }


def _read_health(repo: Repository, *, provider: str, region: str, spec_key: str) -> dict[str, object] | None:
    action_store = store(repo)
    oid = action_store.read_ref(provider_health_ref(pool="default", provider=provider, region=region, spec_key=spec_key))
    if oid is None:
        return None
    try:
        payload = json.loads(action_store.read_blob_payload(oid).decode())
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_health(repo: Repository, payload: dict[str, object]) -> dict[str, object]:
    action_store = store(repo)
    blob = Blob.from_data(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    action_store.write_blob(blob)
    action_store.set_ref(
        provider_health_ref(
            pool="default",
            provider=str(payload["provider"]),
            region=str(payload["region"]),
            spec_key=str(payload["spec_key"]),
        ),
        blob.id,
    )
    return payload


def _now(now_s: int | None) -> int:
    return int(time.time()) if now_s is None else now_s
