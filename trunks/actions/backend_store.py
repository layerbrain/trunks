from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

from trunks.backend import Backend
from trunks.ids import ObjectId
from trunks.objects import Blob, parse_canonical_object
from trunks.repository import Repository
from trunks.refs import Ref, normalize_ref
from trunks.url import backend_from_storage, backend_from_url


T = TypeVar("T")

_BACKENDS: dict[tuple[str, str], tuple[Backend, "_BackendRunner"]] = {}


class ActionsStore:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.backend, self.runner = _actions_backend(repo)

    @property
    def shared(self) -> bool:
        return self.backend is not None

    def write_blob(self, blob: Blob) -> None:
        if self.backend is None:
            self.repo.put_object(blob.id, blob.canonical())
            return
        self.runner.submit(self.backend.write_object(blob.id, blob.canonical()))

    def read_blob_payload(self, oid: ObjectId) -> bytes:
        if self.backend is None:
            return self.repo.blob_payload(oid)
        data = self.runner.submit(self.backend.read_object(oid))
        kind, payload = parse_canonical_object(data)
        if kind != "blob":
            raise ValueError(f"expected blob, got {kind}")
        return payload

    def read_object(self, oid: ObjectId) -> bytes:
        if self.backend is None:
            return self.repo.object_data(oid)
        return self.runner.submit(self.backend.read_object(oid))

    def has_object(self, oid: ObjectId) -> bool:
        if self.backend is None:
            try:
                self.repo.object_data(oid)
            except Exception:
                return False
            return True
        return self.runner.submit(self.backend.has_object(oid))

    def read_ref(self, name: str) -> ObjectId | None:
        if self.backend is None:
            return self.repo.ref(name)
        return self.runner.submit(self.backend.read_ref(name))

    def set_ref(self, name: str, oid: ObjectId) -> None:
        if self.backend is None:
            self.repo.set_ref(name, oid)
            return
        while True:
            expected = self.runner.submit(self.backend.read_ref(name))
            if self.runner.submit(self.backend.cas_ref(name, expected, oid)):
                return

    def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        if self.backend is None:
            return self.repo.cas_ref(name, expected, new)
        return self.runner.submit(self.backend.cas_ref(name, expected, new))

    def delete_ref(self, name: str) -> None:
        if self.backend is None:
            self.repo.delete_ref(name)
            return
        delete_ref = getattr(self.backend, "delete_ref", None)
        if delete_ref is not None:
            try:
                self.runner.submit(delete_ref(name))
            except NotImplementedError:
                return

    def list_refs(self, prefix: str = "") -> list[tuple[str, ObjectId]]:
        normalized = normalize_ref(prefix) if prefix else ""
        if self.backend is None:
            refs = self.repo.list_refs()
            return [(name, oid) for name, oid in refs if not normalized or name.startswith(normalized)]
        return self.runner.submit(_collect_refs(self.backend, normalized))


def store(repo: Repository) -> ActionsStore:
    return ActionsStore(repo)


def repo_root(repo: Repository) -> str:
    return f"refs/actions/repos/{repo.name}"


def pool_root(pool: str = "default") -> str:
    return f"refs/actions/pools/{pool}"


def run_state_ref(repo: Repository, run: str) -> str:
    return f"{repo_root(repo)}/runs/{run}/state"


def run_prefix(repo: Repository, run: str) -> str:
    return f"{repo_root(repo)}/runs/{run}/"


def run_logs_prefix(repo: Repository, run: str) -> str:
    return f"{repo_root(repo)}/runs/{run}/logs/"


def run_log_ref(repo: Repository, run: str, attempt: int, seq: int) -> str:
    return f"{repo_root(repo)}/runs/{run}/logs/{attempt}/{seq:020d}"


def run_artifacts_prefix(repo: Repository, run: str) -> str:
    return f"{repo_root(repo)}/runs/{run}/artifacts/"


def run_artifact_ref(repo: Repository, run: str, name: str) -> str:
    return f"{run_artifacts_prefix(repo, run)}{name}"


def run_index_by_repo_prefix(repo: Repository) -> str:
    return f"{repo_root(repo)}/index/runs/by-repo/"


def run_index_by_status_prefix(repo: Repository, status: str | None = None) -> str:
    base = f"{repo_root(repo)}/index/runs/by-status/"
    return base if status is None else f"{base}{status}/"


def run_queue_ref(repo: Repository, *, spec_key: str, route_bucket: str, shard: str, run: str) -> str:
    return f"{repo_root(repo)}/queue/{spec_key}/{route_bucket}/{shard}/{run}"


def run_queue_prefix(repo: Repository) -> str:
    return f"{repo_root(repo)}/queue/"


def workflow_run_state_ref(repo: Repository, workflow_run: str) -> str:
    return f"{repo_root(repo)}/workflow-runs/{workflow_run}/state"


def workflow_run_prefix(repo: Repository, workflow_run: str) -> str:
    return f"{repo_root(repo)}/workflow-runs/{workflow_run}/"


def workflow_run_index_by_repo_prefix(repo: Repository) -> str:
    return f"{repo_root(repo)}/index/workflow-runs/by-repo/"


def workflow_run_index_by_status_prefix(repo: Repository, status: str | None = None) -> str:
    base = f"{repo_root(repo)}/index/workflow-runs/by-status/"
    return base if status is None else f"{base}{status}/"


def workflow_run_job_ref(repo: Repository, workflow_run: str, job: str, index: int) -> str:
    return f"{repo_root(repo)}/workflow-runs/{workflow_run}/jobs/{job}/{index}"


def secret_ref(repo: Repository, name: str) -> str:
    return f"{repo_root(repo)}/secrets/{name}"


def secret_prefix(repo: Repository) -> str:
    return f"{repo_root(repo)}/secrets/"


def capacity_limit_ref(*, pool: str, region: str, spec_key: str) -> str:
    return f"{pool_root(pool)}/capacity/{region}/{spec_key}/limit"


def capacity_slot_ref(*, pool: str, region: str, spec_key: str, slot: int) -> str:
    return f"{pool_root(pool)}/capacity/{region}/{spec_key}/slots/{slot:06d}"


def capacity_prefix(pool: str = "default") -> str:
    return f"{pool_root(pool)}/capacity/"


def provider_health_ref(*, pool: str, provider: str, region: str, spec_key: str) -> str:
    return f"{pool_root(pool)}/provider-health/{provider}/{region}/{spec_key}/latest"


def provider_health_prefix(pool: str = "default") -> str:
    return f"{pool_root(pool)}/provider-health/"


async def _collect_refs(backend: Backend, prefix: str) -> list[tuple[str, ObjectId]]:
    refs: list[tuple[str, ObjectId]] = []
    async for ref in backend.list_refs(prefix):
        if ref.oid is not None:
            refs.append((ref.name, ref.oid))
    return refs


def _actions_backend(repo: Repository) -> tuple[Backend | None, "_BackendRunner | None"]:
    profile = repo.primary_storage_profile()
    backend = backend_from_storage(profile, repo.name) if profile is not None else backend_from_url(repo.backend_url())
    if backend is None:
        return None, None
    key = (str(repo.path), backend.url.raw)
    cached = _BACKENDS.get(key)
    if cached is not None:
        return cached
    runner = _BackendRunner()
    cached = (backend, runner)
    _BACKENDS[key] = cached
    return cached


class _BackendRunner:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, awaitable: Awaitable[T]) -> T:
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop).result()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
