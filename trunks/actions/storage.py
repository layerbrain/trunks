from __future__ import annotations

import json
import zlib
import time

from trunks.ids import ObjectId, ulid
from trunks.objects import Blob
from trunks.repository import Repository

from .backend_store import (
    run_index_by_repo_prefix,
    run_index_by_status_prefix,
    run_log_ref,
    run_logs_prefix,
    run_queue_prefix,
    run_queue_ref,
    run_state_ref,
    store,
    workflow_run_index_by_repo_prefix,
    workflow_run_index_by_status_prefix,
    workflow_run_prefix,
    workflow_run_state_ref,
)
from .state import Run

TERMINAL_PHASES = frozenset({"succeeded", "failed", "canceled", "skipped"})
PRUNE_SCHEMA = "trunks.actions.prune.v1"
_CROCKFORD = {char: index for index, char in enumerate("0123456789ABCDEFGHJKMNPQRSTVWXYZ")}


def persist_run(repo: Repository, run: Run) -> str:
    return persist_run_payload(repo, run.to_dict())


def persist_run_payload(repo: Repository, payload: dict[str, object], *, expected: ObjectId | None = None) -> str | None:
    action_store = store(repo)
    run = str(payload["id"])
    _persist_log_chunks(repo, run, payload)
    state_payload = _state_payload(payload)
    blob = _run_blob(state_payload)
    action_store.write_blob(blob)
    if expected is None:
        previous = action_store.read_ref(run_state_ref(repo, run))
        action_store.set_ref(run_state_ref(repo, run), blob.id)
    else:
        previous = expected
        if not action_store.cas_ref(run_state_ref(repo, run), expected, blob.id):
            return None
    _refresh_indexes(repo, run, blob.id, state_payload, previous=previous)
    return blob.id.value


def load_run(repo: Repository, run: str) -> dict[str, object]:
    action_store = store(repo)
    oid = action_store.read_ref(run_state_ref(repo, run))
    if oid is None:
        raise KeyError(f"unknown action run {run!r}")
    payload = json.loads(action_store.read_blob_payload(oid).decode())
    payload["logs"] = _load_log_chunks(repo, run)
    return payload


def list_runs(repo: Repository, *, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, object]]:
    action_store = store(repo)
    prefix = run_index_by_status_prefix(repo, status) if status else run_index_by_repo_prefix(repo)
    refs = action_store.list_refs(prefix)
    refs.sort(key=lambda item: item[0], reverse=True)
    window = refs[offset : offset + limit]
    runs: list[dict[str, object]] = []
    for _name, oid in window:
        payload = _load_payload(repo, oid)
        if payload is not None:
            run = payload.get("id")
            if isinstance(run, str):
                payload["logs"] = _load_log_chunks(repo, run)
            runs.append(payload)
    return runs


def pending_run_ids(repo: Repository, *, limit: int = 50) -> list[str]:
    action_store = store(repo)
    refs = [name for name, _oid in action_store.list_refs(run_queue_prefix(repo))]
    refs.sort()
    return [name.rsplit("/", 1)[-1] for name in refs[:limit]]


def claim_run(
    repo: Repository,
    run: str,
    *,
    executor: str,
    provider: str,
    spec_key: str,
    region: str | None,
    requested_region: str | None,
    ttl_s: int = 90,
    now_s: int | None = None,
) -> dict[str, object] | None:
    action_store = store(repo)
    expected = action_store.read_ref(run_state_ref(repo, run))
    if expected is None:
        return None
    payload = _load_payload(repo, expected)
    if payload is None:
        return None
    state = payload.get("state")
    if not isinstance(state, dict):
        return None
    now = int(time.time()) if now_s is None else now_s
    phase = state.get("phase")
    expires_at = state.get("expires_at")
    expired = phase == "running" and isinstance(expires_at, int) and expires_at <= now
    if phase != "pending" and not expired:
        return None
    attempt = state.get("attempt")
    next_attempt = attempt + 1 if expired and isinstance(attempt, int) else 1
    token = ulid()
    claimed = dict(payload)
    claimed["state"] = {
        **state,
        "phase": "running",
        "token": token,
        "expires_at": now + ttl_s,
        "attempt": next_attempt,
        "executor": executor,
        "provider": provider,
        "spec_key": spec_key,
        "requested_region": requested_region,
        "region": region,
    }
    return claimed if persist_run_payload(repo, claimed, expected=expected) is not None else None


def complete_run(
    repo: Repository,
    payload: dict[str, object],
    *,
    token: str,
    phase: str,
    logs: list[dict[str, object]],
    result: dict[str, object] | None,
) -> bool:
    run = str(payload["id"])
    action_store = store(repo)
    expected = action_store.read_ref(run_state_ref(repo, run))
    if expected is None:
        return False
    current = _load_payload(repo, expected)
    if current is None:
        return False
    state = current.get("state")
    if not isinstance(state, dict) or state.get("token") != token:
        return False
    completed = dict(current)
    _write_log_chunks(repo, run, _attempt_from_state(state), logs)
    completed["logs"] = []
    completed["result"] = result
    completed["artifacts"] = payload.get("artifacts", current.get("artifacts", []))
    completed["state"] = {
        **state,
        "phase": phase,
        "token": None,
        "expires_at": None,
    }
    return persist_run_payload(repo, completed, expected=expected) is not None


def release_run_claim(repo: Repository, payload: dict[str, object], *, token: str) -> bool:
    run = str(payload["id"])
    action_store = store(repo)
    expected = action_store.read_ref(run_state_ref(repo, run))
    if expected is None:
        return False
    current = _load_payload(repo, expected)
    if current is None:
        return False
    state = current.get("state")
    if not isinstance(state, dict) or state.get("token") != token:
        return False
    released = dict(current)
    released["state"] = {
        **state,
        "phase": "pending",
        "token": None,
        "expires_at": None,
    }
    return persist_run_payload(repo, released, expected=expected) is not None


def append_run_logs(repo: Repository, run: str, *, token: str, logs: list[dict[str, object]], attempts: int = 5) -> bool:
    if not logs:
        return True
    action_store = store(repo)
    for _attempt in range(attempts):
        expected = action_store.read_ref(run_state_ref(repo, run))
        if expected is None:
            return False
        current = _load_payload(repo, expected)
        if current is None:
            return False
        state = current.get("state")
        if not isinstance(state, dict) or state.get("phase") != "running" or state.get("token") != token:
            return False
        attempt = _attempt_from_state(state)
        seen = {
            (item.get("stream"), item.get("seq"))
            for item in _load_log_chunks_for_attempt(repo, run, attempt)
            if isinstance(item, dict)
        }
        next_logs = [item for item in logs if (item.get("stream"), item.get("seq")) not in seen]
        if not next_logs:
            return True
        _write_log_chunks(repo, run, attempt, next_logs)
        return True
    return False


def heartbeat_run(repo: Repository, run: str, *, token: str, ttl_s: int = 90, now_s: int | None = None) -> bool:
    action_store = store(repo)
    expected = action_store.read_ref(run_state_ref(repo, run))
    if expected is None:
        return False
    current = _load_payload(repo, expected)
    if current is None:
        return False
    state = current.get("state")
    if not isinstance(state, dict) or state.get("phase") != "running" or state.get("token") != token:
        return False
    now = int(time.time()) if now_s is None else now_s
    updated = dict(current)
    updated["state"] = {**state, "expires_at": now + ttl_s}
    return persist_run_payload(repo, updated, expected=expected) is not None


def cancel_run(repo: Repository, run: str) -> dict[str, object] | None:
    action_store = store(repo)
    expected = action_store.read_ref(run_state_ref(repo, run))
    if expected is None:
        return None
    current = _load_payload(repo, expected)
    if current is None:
        return None
    state = current.get("state")
    if not isinstance(state, dict):
        return None
    if state.get("phase") in {"succeeded", "failed", "canceled", "skipped"}:
        return current
    canceled = dict(current)
    canceled["state"] = {**state, "phase": "canceled", "token": None, "expires_at": None}
    return canceled if persist_run_payload(repo, canceled, expected=expected) is not None else None


def repair_indexes(repo: Repository) -> dict[str, object]:
    repaired = 0
    action_store = store(repo)
    for name, oid in action_store.list_refs(f"{run_queue_prefix(repo).rsplit('/queue/', 1)[0]}/runs/"):
        if not name.endswith("/state"):
            continue
        payload = _load_payload(repo, oid)
        if payload is None:
            continue
        run = payload.get("id")
        if not isinstance(run, str):
            continue
        _refresh_indexes(repo, run, oid, payload, previous=None)
        repaired += 1
    return {"_schema_version": "trunks.actions.index_repair.v1", "object": "index_repair", "repaired": repaired}


def prune_runs(
    repo: Repository,
    *,
    older_than_s: int | None = None,
    keep_last: int | None = None,
    dry_run: bool = False,
    clean_objects: bool = False,
    now_s: float | None = None,
) -> dict[str, object]:
    if older_than_s is None and keep_last is None:
        raise ValueError("prune requires --older-than or --keep-last")
    if older_than_s is not None and older_than_s < 0:
        raise ValueError("older_than_s must be >= 0")
    if keep_last is not None and keep_last < 0:
        raise ValueError("keep_last must be >= 0")

    now = time.time() if now_s is None else now_s
    workflow_runs = _workflow_run_candidates(repo)
    workflow_pruned = _select_prunable(workflow_runs, older_than_s=older_than_s, keep_last=keep_last, now_s=now)
    workflow_pruned_ids = {item["id"] for item in workflow_pruned}
    retained_workflow_run_ids = {
        run
        for item in workflow_runs
        if item["id"] not in workflow_pruned_ids
        for run in _workflow_job_runs(item["payload"])
    }

    action_runs = _action_run_candidates(repo)
    action_pruned = _select_prunable(action_runs, older_than_s=older_than_s, keep_last=keep_last, now_s=now)
    action_pruned_ids = {
        item["id"]
        for item in action_pruned
        if item["id"] not in retained_workflow_run_ids
    }
    for item in workflow_pruned:
        action_pruned_ids.update(_workflow_job_runs(item["payload"]))

    if not dry_run:
        for workflow_run in sorted(workflow_pruned_ids):
            _delete_workflow_run_refs(repo, workflow_run)
        for run in sorted(action_pruned_ids):
            _delete_action_run_refs(repo, run)
    removed_objects = repo.clean_unreachable() if clean_objects and not dry_run else 0
    return {
        "_schema_version": PRUNE_SCHEMA,
        "object": "actions_prune",
        "dry_run": dry_run,
        "clean_objects": clean_objects,
        "older_than_s": older_than_s,
        "keep_last": keep_last,
        "runs": sorted(action_pruned_ids),
        "workflow_runs": sorted(workflow_pruned_ids),
        "deleted_run_refs": 0 if dry_run else len(action_pruned_ids),
        "deleted_workflow_run_refs": 0 if dry_run else len(workflow_pruned_ids),
        "removed_objects": removed_objects,
    }


def _run_blob(payload: dict[str, object]) -> Blob:
    return Blob.from_data(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _refresh_indexes(
    repo: Repository,
    run: str,
    oid: ObjectId,
    payload: dict[str, object],
    *,
    previous: ObjectId | None,
) -> None:
    action_store = store(repo)
    previous_phase = _phase(repo, previous)
    phase = _payload_phase(payload)
    if previous_phase is not None and previous_phase != phase:
        action_store.delete_ref(f"{run_index_by_status_prefix(repo, previous_phase)}{run}")
    action_store.set_ref(f"{run_index_by_repo_prefix(repo)}{run}", oid)
    action_store.set_ref(f"{run_index_by_status_prefix(repo, phase)}{run}", oid)
    _refresh_queue_ref(repo, run, payload, previous_phase=previous_phase, phase=phase, oid=oid)


def _phase(repo: Repository, oid: ObjectId | None) -> str | None:
    if oid is None:
        return None
    payload = _load_payload(repo, oid)
    if payload is None:
        return None
    return _payload_phase(payload)


def _load_payload(repo: Repository, oid: ObjectId) -> dict[str, object] | None:
    try:
        payload = json.loads(store(repo).read_blob_payload(oid).decode())
    except (KeyError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _payload_phase(payload: dict[str, object]) -> str:
    state = payload.get("state")
    if isinstance(state, dict):
        phase = state.get("phase")
        if isinstance(phase, str):
            return phase
    return "unknown"


def _action_run_candidates(repo: Repository) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    action_store = store(repo)
    for name, oid in action_store.list_refs(f"{run_queue_prefix(repo).rsplit('/queue/', 1)[0]}/runs/"):
        if not name.endswith("/state"):
            continue
        payload = _load_payload(repo, oid)
        if payload is None:
            continue
        run = payload.get("id")
        if not isinstance(run, str):
            continue
        phase = _payload_phase(payload)
        if phase not in TERMINAL_PHASES:
            continue
        candidates.append({"id": run, "phase": phase, "created_at_s": _ulid_time_s(run), "payload": payload})
    candidates.sort(key=lambda item: str(item["id"]), reverse=True)
    return candidates


def _workflow_run_candidates(repo: Repository) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    action_store = store(repo)
    for name, oid in action_store.list_refs(f"{run_queue_prefix(repo).rsplit('/queue/', 1)[0]}/workflow-runs/"):
        if not name.endswith("/state"):
            continue
        payload = _load_payload(repo, oid)
        if payload is None:
            continue
        workflow_run = payload.get("id")
        phase = payload.get("phase")
        if not isinstance(workflow_run, str) or not isinstance(phase, str) or phase not in TERMINAL_PHASES:
            continue
        candidates.append({"id": workflow_run, "phase": phase, "created_at_s": _ulid_time_s(workflow_run), "payload": payload})
    candidates.sort(key=lambda item: str(item["id"]), reverse=True)
    return candidates


def _select_prunable(
    candidates: list[dict[str, object]],
    *,
    older_than_s: int | None,
    keep_last: int | None,
    now_s: float,
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for index, item in enumerate(candidates):
        old_enough = older_than_s is None or (
            isinstance(item.get("created_at_s"), float) and now_s - item["created_at_s"] >= older_than_s
        )
        past_keep = keep_last is None or index >= keep_last
        if old_enough and past_keep:
            selected.append(item)
    return selected


def _workflow_job_runs(payload: dict[str, object]) -> set[str]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return set()
    return {str(job["run"]) for job in jobs if isinstance(job, dict) and isinstance(job.get("run"), str)}


def _delete_action_run_refs(repo: Repository, run: str) -> None:
    action_store = store(repo)
    for name, _oid in action_store.list_refs():
        if name.startswith(run_prefix := f"{run_queue_prefix(repo).rsplit('/queue/', 1)[0]}/runs/{run}/"):
            action_store.delete_ref(name)
        elif name.startswith(f"{run_index_by_status_prefix(repo)}") and name.endswith(f"/{run}"):
            action_store.delete_ref(name)
        elif name.startswith(run_index_by_repo_prefix(repo)) and name.endswith(f"/{run}"):
            action_store.delete_ref(name)


def _delete_workflow_run_refs(repo: Repository, workflow_run: str) -> None:
    action_store = store(repo)
    for name, _oid in action_store.list_refs():
        if name.startswith(workflow_run_prefix(repo, workflow_run)):
            action_store.delete_ref(name)
        elif name.startswith(workflow_run_index_by_status_prefix(repo)) and name.endswith(f"/{workflow_run}"):
            action_store.delete_ref(name)
        elif name.startswith(workflow_run_index_by_repo_prefix(repo)) and name.endswith(f"/{workflow_run}"):
            action_store.delete_ref(name)


def _ulid_time_s(value: str) -> float | None:
    if len(value) != 26:
        return None
    timestamp = 0
    for char in value[:10].upper():
        if char not in _CROCKFORD:
            return None
        timestamp = (timestamp << 5) | _CROCKFORD[char]
    return timestamp / 1000


def _state_payload(payload: dict[str, object]) -> dict[str, object]:
    stripped = dict(payload)
    stripped["logs"] = []
    return stripped


def _persist_log_chunks(repo: Repository, run: str, payload: dict[str, object]) -> None:
    logs = payload.get("logs")
    if not isinstance(logs, list):
        return
    state = payload.get("state")
    attempt = _attempt_from_state(state) if isinstance(state, dict) else 0
    _write_log_chunks(repo, run, attempt, [item for item in logs if isinstance(item, dict)])


def _write_log_chunks(repo: Repository, run: str, attempt: int, logs: list[dict[str, object]]) -> None:
    action_store = store(repo)
    for item in logs:
        seq = item.get("seq")
        if not isinstance(seq, int):
            continue
        blob = Blob.from_data(json.dumps(item, sort_keys=True, separators=(",", ":")).encode())
        action_store.write_blob(blob)
        action_store.set_ref(run_log_ref(repo, run, attempt, seq), blob.id)


def _load_log_chunks(repo: Repository, run: str) -> list[dict[str, object]]:
    action_store = store(repo)
    logs: list[tuple[str, dict[str, object]]] = []
    for name, oid in action_store.list_refs(run_logs_prefix(repo, run)):
        payload = _load_payload(repo, oid)
        if payload is not None:
            logs.append((name, payload))
    logs.sort(key=lambda item: item[0])
    return [payload for _name, payload in logs]


def _load_log_chunks_for_attempt(repo: Repository, run: str, attempt: int) -> list[dict[str, object]]:
    action_store = store(repo)
    logs: list[dict[str, object]] = []
    for _name, oid in action_store.list_refs(f"{run_logs_prefix(repo, run)}{attempt}/"):
        payload = _load_payload(repo, oid)
        if payload is not None:
            logs.append(payload)
    return logs


def _attempt_from_state(state: dict[str, object]) -> int:
    attempt = state.get("attempt")
    return attempt if isinstance(attempt, int) and attempt >= 0 else 0


def _refresh_queue_ref(
    repo: Repository,
    run: str,
    payload: dict[str, object],
    *,
    previous_phase: str | None,
    phase: str,
    oid: ObjectId,
) -> None:
    action_store = store(repo)
    if previous_phase == "pending" and phase != "pending":
        for name, _ref_oid in action_store.list_refs(run_queue_prefix(repo)):
            if name.endswith(f"/{run}"):
                action_store.delete_ref(name)
    if phase != "pending":
        return
    state = payload.get("state")
    spec_key = None
    requested_region = None
    if isinstance(state, dict):
        spec_key = state.get("spec_key")
        requested_region = state.get("requested_region")
    if not isinstance(spec_key, str) or not spec_key:
        return
    route_bucket = "any"
    if isinstance(requested_region, str) and payload.get("region_pin") == "hard":
        route_bucket = requested_region
    shard = f"{zlib.crc32(run.encode()) % 64:04x}"
    action_store.set_ref(run_queue_ref(repo, spec_key=spec_key, route_bucket=route_bucket, shard=shard, run=run), oid)
