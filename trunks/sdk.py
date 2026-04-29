from __future__ import annotations

import os
import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from .errors import RepositoryNotFound, TrunksError
from .ids import ObjectId
from .repository import Repository
from .storage import Storage
from .config import resolve_backend_url
from . import audit, webhooks

ListResponse = dict[str, Any]
Resource = dict[str, Any]
T = TypeVar("T")


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _list_response(items: list[Resource], *, limit: int | None = 100, offset: int = 0) -> ListResponse:
    safe_offset = max(offset, 0)
    safe_limit = len(items) if limit is None else max(limit, 0)
    data = items[safe_offset:safe_offset + safe_limit]
    return {
        "object": "list",
        "data": data,
        "limit": safe_limit,
        "offset": safe_offset,
        "total_count": len(items),
        "has_more": safe_offset + len(data) < len(items),
    }


def _managed_repo_root(name: str) -> Path:
    home = Path(os.environ.get("TRUNKS_HOME", str(Path.home() / ".trunks")))
    return home / "repos" / name


def _branch_record(repo: Repository, ref_name: str, oid: ObjectId) -> Resource:
    name = ref_name.removeprefix("refs/heads/")
    return {
        "object": "branch",
        "id": name,
        "name": name,
        "ref": ref_name,
        "head": oid.value,
        "current": name == repo.current_branch,
    }


def _tag_record(ref_name: str, oid: ObjectId) -> Resource:
    name = ref_name.removeprefix("refs/tags/")
    return {
        "object": "tag",
        "id": name,
        "name": name,
        "ref": ref_name,
        "target": oid.value,
    }


def _repo_record(repo: Repository) -> Resource:
    head = repo.ref(repo.current_branch)
    return {
        "object": "repo",
        "id": repo.name,
        "name": repo.name,
        "path": str(repo.root),
        "repo_data_path": str(repo.path),
        "current_branch": repo.current_branch,
        "head": head.value if head else None,
        "backend": repo.backend_url(),
        "storage": [_storage_record(item) for item in repo.storage_targets()],
    }


def _storage_record(item: dict[str, object]) -> Resource:
    name = str(item.get("name", ""))
    return {
        "object": "storage_target",
        "id": name,
        **item,
    }


def _webhook_record(item: dict[str, object]) -> Resource:
    hook_id = str(item.get("id", ""))
    return {
        "object": "webhook",
        **item,
        "id": hook_id,
    }


def _audit_record(item: dict[str, object]) -> Resource:
    event_id = str(item.get("id", ""))
    return {
        "object": "audit_event",
        **item,
        "id": event_id,
    }


class Trunks:
    def __init__(self, *, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self._async_mode = _has_running_loop()
        self.repos = ReposResource(self)
        self.branches = BranchesResource(self)
        self.tags = TagsResource(self)
        self.storage = StorageResource(self)
        self.webhooks = WebhooksResource(self)
        self.audit = AuditResource(self)

    def _repo(self) -> Repository:
        return Repository.find(self.cwd)

    def _auto(self, factory: Callable[[], T]) -> T | Awaitable[T]:
        if self._async_mode:
            async def _run() -> T:
                return factory()
            return _run()
        return factory()


class ReposResource:
    def __init__(self, client: Trunks) -> None:
        self._client = client

    def list(self, *, limit: int | None = 100, offset: int = 0) -> ListResponse | Awaitable[ListResponse]:
        return self._client._auto(lambda: _list_response(
            [_repo_record(repo) for repo in self._discover()],
            limit=limit,
            offset=offset,
        ))

    def get(self, *, name: str | None = None) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._get(name=name))

    def create(
        self,
        *,
        name: str,
        path: str | Path | None = None,
        backend: str | None = None,
    ) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._create(name=name, path=path, backend=backend))

    def update(
        self,
        *,
        name: str | None = None,
        backend: str,
    ) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._update(name=name, backend=backend))

    def delete(self, *, name: str | None = None) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._delete(name=name))

    def _get(self, *, name: str | None) -> Resource:
        repo = self._find(name)
        return _repo_record(repo)

    def _create(self, *, name: str, path: str | Path | None, backend: str | None) -> Resource:
        root = (Path(path).expanduser() if path else _managed_repo_root(name)).resolve()
        root.mkdir(parents=True, exist_ok=True)
        repo = Repository.init(root, name=name, backend=resolve_backend_url(backend, name) if backend else None)
        return _repo_record(repo)

    def _update(self, *, name: str | None, backend: str) -> Resource:
        repo = self._find(name)
        repo.set_backend_url(resolve_backend_url(backend, repo.name) or "")
        return _repo_record(repo)

    def _delete(self, *, name: str | None) -> Resource:
        repo = self._find(name)
        repo_name = repo.name
        if repo.path.exists():
            repo.path.unlink()
        return {"object": "repo", "id": repo_name, "name": repo_name, "deleted": True}

    def _find(self, name: str | None) -> Repository:
        if name is None:
            return self._client._repo()
        for repo in self._discover():
            if repo.name == name:
                return repo
        raise RepositoryNotFound(f"repo not found: {name}")

    def _discover(self) -> list[Repository]:
        repos: dict[str, Repository] = {}
        try:
            repo = self._client._repo()
        except RepositoryNotFound:
            repo = None
        if repo is not None:
            repos[str(repo.path)] = repo
        home = Path(os.environ.get("TRUNKS_HOME", str(Path.home() / ".trunks")))
        for path in sorted((home / "repos").glob("*/.trunks/*.trunk")):
            try:
                discovered = Repository(path)
                discovered.name
            except TrunksError:
                continue
            repos.setdefault(str(discovered.path), discovered)
        return sorted(repos.values(), key=lambda item: item.name)


class BranchesResource:
    def __init__(self, client: Trunks) -> None:
        self._client = client

    def list(self, *, limit: int | None = 100, offset: int = 0) -> ListResponse | Awaitable[ListResponse]:
        return self._client._auto(lambda: _list_response(self._records(), limit=limit, offset=offset))

    def create(
        self,
        *,
        name: str,
        from_ref: str | None = None,
        from_: str | None = None,
    ) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._create(name=name, from_ref=from_ref, from_=from_))

    def _create(self, *, name: str, from_ref: str | None, from_: str | None) -> Resource:
        repo = self._client._repo()
        repo.create_branch(name, start=from_ref or from_, switch=False)
        oid = repo.ref(name)
        if oid is None:
            raise RepositoryNotFound(f"branch not found after create: {name}")
        return _branch_record(repo, f"refs/heads/{name}", oid)

    def get(self, *, name: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._get(name=name))

    def _get(self, *, name: str) -> Resource:
        repo = self._client._repo()
        oid = repo.ref(name)
        if oid is None:
            raise RepositoryNotFound(f"branch not found: {name}")
        return _branch_record(repo, f"refs/heads/{name}", oid)

    def update(self, *, name: str, from_ref: str | None = None, from_: str | None = None) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._update(name=name, from_ref=from_ref, from_=from_))

    def _update(self, *, name: str, from_ref: str | None, from_: str | None) -> Resource:
        repo = self._client._repo()
        target = repo.ref(from_ref or from_ or repo.current_branch)
        if target is None:
            raise TrunksError(f"unknown ref: {from_ref or from_ or repo.current_branch}")
        repo.set_ref(f"refs/heads/{name}", target)
        return _branch_record(repo, f"refs/heads/{name}", target)

    def switch(self, *, name: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._switch(name=name))

    def _switch(self, *, name: str) -> Resource:
        repo = self._client._repo()
        repo.checkout(name)
        oid = repo.ref(name)
        if oid is None:
            raise RepositoryNotFound(f"branch not found after switch: {name}")
        return _branch_record(repo, f"refs/heads/{name}", oid)

    def delete(self, *, name: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._delete(name=name))

    def _delete(self, *, name: str) -> Resource:
        repo = self._client._repo()
        if name == repo.current_branch:
            raise TrunksError(f"cannot delete current branch: {name}")
        repo.delete_ref(name)
        return {"object": "branch", "id": name, "name": name, "deleted": True}

    def _records(self) -> list[Resource]:
        repo = self._client._repo()
        return [
            _branch_record(repo, ref_name, oid)
            for ref_name, oid in repo.list_refs()
            if ref_name.startswith("refs/heads/")
        ]


class TagsResource:
    def __init__(self, client: Trunks) -> None:
        self._client = client

    def list(self, *, limit: int | None = 100, offset: int = 0) -> ListResponse | Awaitable[ListResponse]:
        return self._client._auto(lambda: _list_response(self._records(), limit=limit, offset=offset))

    def create(self, *, name: str, at: str | None = None) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._create(name=name, at=at))

    def _create(self, *, name: str, at: str | None) -> Resource:
        repo = self._client._repo()
        target = repo.ref(at or repo.current_branch)
        if target is None:
            raise TrunksError(f"unknown ref: {at or repo.current_branch}")
        repo.set_ref(f"refs/tags/{name}", target)
        return _tag_record(f"refs/tags/{name}", target)

    def get(self, *, name: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._get(name=name))

    def _get(self, *, name: str) -> Resource:
        repo = self._client._repo()
        target = repo.ref(f"refs/tags/{name}")
        if target is None:
            raise TrunksError(f"unknown tag: {name}")
        return _tag_record(f"refs/tags/{name}", target)

    def update(self, *, name: str, at: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._update(name=name, at=at))

    def _update(self, *, name: str, at: str) -> Resource:
        repo = self._client._repo()
        target = repo.ref(at)
        if target is None:
            raise TrunksError(f"unknown ref: {at}")
        repo.set_ref(f"refs/tags/{name}", target)
        return _tag_record(f"refs/tags/{name}", target)

    def delete(self, *, name: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._delete(name=name))

    def _delete(self, *, name: str) -> Resource:
        repo = self._client._repo()
        repo.delete_ref(f"refs/tags/{name}")
        return {"object": "tag", "id": name, "name": name, "deleted": True}

    def _records(self) -> list[Resource]:
        repo = self._client._repo()
        return [
            _tag_record(ref_name, oid)
            for ref_name, oid in repo.list_refs()
            if ref_name.startswith("refs/tags/")
        ]


class StorageResource:
    def __init__(self, client: Trunks) -> None:
        self._client = client

    def list(self, *, limit: int | None = 100, offset: int = 0) -> ListResponse | Awaitable[ListResponse]:
        return self._client._auto(lambda: _list_response(self._records(), limit=limit, offset=offset))

    def create(self, *, name: str, url: str, mirror: bool = False) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._upsert(name=name, url=url, mirror=mirror))

    def get(self, *, name: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._get(name=name))

    def update(self, *, name: str, url: str, mirror: bool = False) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._upsert(name=name, url=url, mirror=mirror))

    def delete(self, *, name: str) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._delete(name=name))

    def _upsert(self, *, name: str, url: str, mirror: bool) -> Resource:
        repo = self._client._repo()
        resolved = resolve_backend_url(url, repo.name)
        if resolved is None:
            raise TrunksError(f"invalid storage URL: {url}")
        profile = Storage.from_url(name=name, role="mirror" if mirror else "primary", url=resolved)
        repo.set_storage_profile(profile)
        return _storage_record(profile.public_record(repo.name))

    def _get(self, *, name: str) -> Resource:
        profile = self._client._repo().storage_profile(name)
        if profile is None:
            raise TrunksError(f"unknown storage target: {name}")
        return _storage_record(profile.public_record(self._client._repo().name))

    def _delete(self, *, name: str) -> Resource:
        if not self._client._repo().remove_storage(name):
            raise TrunksError(f"unknown storage target: {name}")
        return {"object": "storage_target", "id": name, "name": name, "deleted": True}

    def _records(self) -> list[Resource]:
        return [_storage_record(item) for item in self._client._repo().storage_targets()]


class WebhooksResource:
    def __init__(self, client: Trunks) -> None:
        self._client = client

    def list(self, *, limit: int | None = 100, offset: int = 0) -> ListResponse | Awaitable[ListResponse]:
        return self._client._auto(lambda: _list_response(self._records(), limit=limit, offset=offset))

    def create(self, *, url: str, events: list[str] | None = None) -> Resource | Awaitable[Resource]:
        return self._client._auto(lambda: self._create(url=url, events=events or ["push"]))

    def get(self, *, id: str) -> Resource | Awaitable[Resource]:  # noqa: A002
        return self._client._auto(lambda: self._get(id=id))

    def update(self, *, id: str, url: str | None = None, events: list[str] | None = None) -> Resource | Awaitable[Resource]:  # noqa: A002
        return self._client._auto(lambda: self._update(id=id, url=url, events=events))

    def delete(self, *, id: str) -> Resource | Awaitable[Resource]:  # noqa: A002
        return self._client._auto(lambda: self._delete(id=id))

    def _create(self, *, url: str, events: list[str]) -> Resource:
        hook = webhooks.add(self._client._repo(), url, events)
        return _webhook_record(hook.public_record())

    def _get(self, *, id: str) -> Resource:
        hook = webhooks.get(self._client._repo(), id)
        if hook is None:
            raise TrunksError(f"unknown webhook: {id}")
        return _webhook_record(hook.public_record())

    def _update(self, *, id: str, url: str | None, events: list[str] | None) -> Resource:
        hook = webhooks.update(self._client._repo(), id, url=url, events=events)
        if hook is None:
            raise TrunksError(f"unknown webhook: {id}")
        return _webhook_record(hook.public_record())

    def _delete(self, *, id: str) -> Resource:
        if not webhooks.delete(self._client._repo(), id):
            raise TrunksError(f"unknown webhook: {id}")
        return {"object": "webhook", "id": id, "deleted": True}

    def _records(self) -> list[Resource]:
        return [_webhook_record(hook.public_record()) for hook in webhooks.list_hooks(self._client._repo())]


class AuditResource:
    def __init__(self, client: Trunks) -> None:
        self._client = client

    def list(self, *, limit: int | None = 100, offset: int = 0) -> ListResponse | Awaitable[ListResponse]:
        return self._client._auto(lambda: _list_response(self._records(), limit=limit, offset=offset))

    def _records(self) -> list[Resource]:
        return [_audit_record(event.to_record()) for event in audit.read(self._client._repo())]
