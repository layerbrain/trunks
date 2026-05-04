from __future__ import annotations

import json
import time

from trunks.ids import ObjectId, ulid
from trunks.objects import Blob
from trunks.repository import Repository

from .backend_store import capacity_limit_ref, capacity_prefix, capacity_slot_ref, store
from .storage import list_runs

CAPACITY_SCHEMA = "trunks.actions.capacity_limit.v1"
CAPACITY_SNAPSHOT_SCHEMA = "trunks.actions.capacity_snapshot.v1"


def set_capacity_limit(
    repo: Repository,
    *,
    region: str,
    spec_key: str,
    max_concurrent: int,
    pool: str = "default",
) -> dict[str, object]:
    if max_concurrent < 0:
        raise ValueError("max_concurrent must be >= 0")
    payload = {
        "_schema_version": CAPACITY_SCHEMA,
        "object": "capacity_limit",
        "pool": pool,
        "region": region,
        "spec_key": spec_key,
        "max_concurrent": max_concurrent,
    }
    blob = Blob.from_data(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    action_store = store(repo)
    action_store.write_blob(blob)
    action_store.set_ref(capacity_limit_ref(pool=pool, region=region, spec_key=spec_key), blob.id)
    return payload


def capacity_limit(repo: Repository, *, region: str, spec_key: str, pool: str = "default") -> int | None:
    action_store = store(repo)
    oid = action_store.read_ref(capacity_limit_ref(pool=pool, region=region, spec_key=spec_key))
    if oid is None:
        return None
    payload = json.loads(action_store.read_blob_payload(oid).decode())
    value = payload.get("max_concurrent")
    return value if isinstance(value, int) else None


def capacity_available(repo: Repository, *, region: str, spec_key: str, pool: str = "default") -> bool:
    limit = capacity_limit(repo, region=region, spec_key=spec_key, pool=pool)
    if limit is None:
        return True
    return len(_live_slots(repo, pool=pool, region=region, spec_key=spec_key, now_s=int(time.time()))) < limit


def claim_capacity_slot(
    repo: Repository,
    *,
    region: str,
    spec_key: str,
    owner: str,
    pool: str = "default",
    ttl_s: int = 90,
    now_s: int | None = None,
) -> dict[str, object] | None:
    limit = capacity_limit(repo, region=region, spec_key=spec_key, pool=pool)
    if limit is None:
        return {"pool": pool, "region": region, "spec_key": spec_key, "slot": None, "token": None}
    now = int(time.time()) if now_s is None else now_s
    if len(_live_slots(repo, pool=pool, region=region, spec_key=spec_key, now_s=now)) >= limit:
        return None
    action_store = store(repo)
    token = ulid()
    for slot in range(limit):
        ref = capacity_slot_ref(pool=pool, region=region, spec_key=spec_key, slot=slot)
        expected = action_store.read_ref(ref)
        current = _slot_payload(repo, expected)
        if current is not None and int(current.get("expires_at", 0)) > now:
            continue
        payload = {
            "_schema_version": "trunks.actions.capacity_slot.v1",
            "object": "capacity_slot",
            "pool": pool,
            "region": region,
            "spec_key": spec_key,
            "slot": slot,
            "owner": owner,
            "token": token,
            "expires_at": now + ttl_s,
        }
        blob = Blob.from_data(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        action_store.write_blob(blob)
        if action_store.cas_ref(ref, expected, blob.id):
            return payload
    return None


def release_capacity_slot(repo: Repository, claim: dict[str, object] | None) -> None:
    if not claim or claim.get("slot") is None:
        return
    slot = claim.get("slot")
    pool = str(claim.get("pool") or "default")
    region = claim.get("region")
    spec_key = claim.get("spec_key")
    if not isinstance(slot, int) or not isinstance(region, str) or not isinstance(spec_key, str):
        return
    action_store = store(repo)
    ref = capacity_slot_ref(pool=pool, region=region, spec_key=spec_key, slot=slot)
    oid = action_store.read_ref(ref)
    payload = _slot_payload(repo, oid)
    if payload is not None and payload.get("token") == claim.get("token"):
        action_store.delete_ref(ref)


def heartbeat_capacity_slot(
    repo: Repository,
    claim: dict[str, object] | None,
    *,
    ttl_s: int = 90,
    now_s: int | None = None,
) -> bool:
    if not claim or claim.get("slot") is None:
        return True
    slot = claim.get("slot")
    pool = str(claim.get("pool") or "default")
    region = claim.get("region")
    spec_key = claim.get("spec_key")
    token = claim.get("token")
    if not isinstance(slot, int) or not isinstance(region, str) or not isinstance(spec_key, str) or not isinstance(token, str):
        return False
    action_store = store(repo)
    ref = capacity_slot_ref(pool=pool, region=region, spec_key=spec_key, slot=slot)
    expected = action_store.read_ref(ref)
    payload = _slot_payload(repo, expected)
    if payload is None or payload.get("token") != token:
        return False
    now = int(time.time()) if now_s is None else now_s
    updated = {**payload, "expires_at": now + ttl_s}
    blob = Blob.from_data(json.dumps(updated, sort_keys=True, separators=(",", ":")).encode())
    action_store.write_blob(blob)
    return action_store.cas_ref(ref, expected, blob.id)


def capacity_snapshot(repo: Repository) -> dict[str, object]:
    limits = []
    slots = []
    action_store = store(repo)
    for name, oid in action_store.list_refs(capacity_prefix()):
        payload = json.loads(action_store.read_blob_payload(oid).decode())
        if name.endswith("/limit"):
            limits.append(payload)
        elif "/slots/" in name:
            slots.append(payload)
    return {
        "_schema_version": CAPACITY_SNAPSHOT_SCHEMA,
        "object": "capacity_snapshot",
        "limits": limits,
        "slots": slots,
        "running": list_runs(repo, status="running", limit=10_000),
    }


def _live_slots(repo: Repository, *, pool: str, region: str, spec_key: str, now_s: int) -> list[dict[str, object]]:
    action_store = store(repo)
    prefix = capacity_slot_ref(pool=pool, region=region, spec_key=spec_key, slot=0).rsplit("/", 1)[0] + "/"
    live = []
    for _name, oid in action_store.list_refs(prefix):
        payload = _slot_payload(repo, oid)
        if payload is not None and int(payload.get("expires_at", 0)) > now_s:
            live.append(payload)
    return live


def _slot_payload(repo: Repository, oid: ObjectId | None) -> dict[str, object] | None:
    if oid is None:
        return None
    try:
        payload = json.loads(store(repo).read_blob_payload(oid).decode())
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
