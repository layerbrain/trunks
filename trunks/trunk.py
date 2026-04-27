from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterator, TypeVar

from .backend import Backend
from .config import (
    GlobalConfig,
    RepoConfig,
    backend_from_env,
    env_forces_local_only,
    mirror_backends_from_env,
    repo_name_from_env,
    resolve_backend_url,
)
from .engine import Engine
from .errors import RepositoryNotFound
from .objects import Commit, Identity
from .repository import Repository, Status
from .url import backend_from_storage, backend_from_url

T = TypeVar("T")


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class Trunk:
    def __init__(self, backend: str | Backend | None = None, *, name: str | None = None, branch: str | None = None) -> None:
        if isinstance(backend, str) and "://" not in backend and ":" in backend:
            raise ValueError(
                f"malformed backend URL: {backend!r} (did you mean a scheme like 's3://...'?)"
            )
        self._backend_arg = backend
        self._name = name
        self._branch = branch
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._repository: Repository | None = None
        self._backend: Backend | None = None
        self._engine: Engine | None = None
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._open_lock = threading.Lock()
        self._sync_lock = threading.RLock()
        self._closed = False

    def _auto(self, coro_factory: Callable[[], Awaitable[T]]) -> T | Awaitable[T]:
        if self._closed:
            raise RuntimeError("Trunk: cannot reuse a closed instance; create a new Trunk()")
        if _in_running_loop():
            return coro_factory()
        with self._sync_lock:
            coro = coro_factory()
            if self._sync_loop is None or self._sync_loop.is_closed():
                self._sync_loop = asyncio.new_event_loop()
            return self._sync_loop.run_until_complete(coro)

    @property
    def repository(self) -> Repository:
        self._ensure_open()
        assert self._repository is not None
        return self._repository

    @property
    def engine(self) -> Engine:
        self._ensure_open()
        assert self._engine is not None
        return self._engine

    def write(self, path: str, data: bytes, *, branch: str | None = None) -> None | Awaitable[None]:
        return self._auto(lambda: self.engine.write(path, data, branch=branch))

    def read(self, path: str, *, branch: str | None = None) -> bytes | Awaitable[bytes]:
        return self._auto(lambda: self.engine.read(path, branch=branch))

    def delete(self, path: str, *, branch: str | None = None) -> None | Awaitable[None]:
        return self._auto(lambda: self.engine.delete(path, branch=branch))

    def commit(
        self,
        *,
        message: str,
        author: Identity | None = None,
        committer: Identity | None = None,
        branch: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Commit | Awaitable[Commit]:
        return self._auto(
            lambda: self.engine.commit(
                message=message,
                author=author,
                committer=committer,
                branch=branch,
                labels=labels,
            )
        )

    def push(self) -> None | Awaitable[None]:
        return self._auto(lambda: self.engine.push())

    def pull(self) -> None | Awaitable[None]:
        return self._auto(lambda: self.engine.pull())

    def fetch(self) -> None | Awaitable[None]:
        return self._auto(lambda: self.engine.fetch())

    def status(self) -> Status | Awaitable[Status]:
        return self._auto(lambda: self.engine.status())

    def log(self, *, branch: str | None = None, limit: int = 50) -> Iterator[Commit] | AsyncIterator[Commit]:
        self._ensure_open()
        if _in_running_loop():
            return self.engine.log(branch=branch, limit=limit)
        if self._sync_loop is None:
            self._sync_loop = asyncio.new_event_loop()
        return iter(self._sync_loop.run_until_complete(self.engine.log_list(branch=branch, limit=limit)))

    def open(self) -> "Trunk" | Awaitable["Trunk"]:
        async def _open() -> "Trunk":
            self._ensure_open()
            assert self._engine is not None
            await self._engine.open()
            return self

        return self._auto(_open)

    def close(self) -> None | Awaitable[None]:
        async def _close() -> None:
            try:
                if self._engine is not None:
                    await self._engine.close()
                elif self._backend is not None:
                    await self._backend.__aexit__(None, None, None)
            finally:
                if self._tmpdir is not None:
                    self._tmpdir.cleanup()
                    self._tmpdir = None
                self._repository = None
                self._engine = None
                self._backend = None
                self._closed = True

        async def _close_from_running_loop() -> None:
            await _close()
            with self._sync_lock:
                self._close_sync_loop()

        if _in_running_loop():
            return _close_from_running_loop()
        with self._sync_lock:
            if self._sync_loop is not None and not self._sync_loop.is_closed():
                self._sync_loop.run_until_complete(_close())
                self._close_sync_loop()
            else:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_close())
                finally:
                    loop.close()
        return None

    def _close_sync_loop(self) -> None:
        loop = getattr(self, "_sync_loop", None)
        self._sync_loop = None
        if loop is not None and not loop.is_closed() and not loop.is_running():
            loop.close()

    def __del__(self) -> None:
        tmpdir = getattr(self, "_tmpdir", None)
        if tmpdir is not None:
            tmpdir.cleanup()
            self._tmpdir = None
        self._close_sync_loop()

    def __enter__(self) -> "Trunk":
        if _in_running_loop():
            raise RuntimeError(
                "Trunk: synchronous 'with Trunk()' cannot be used inside a running asyncio loop. "
                "Use 'async with Trunk()' and await operations instead."
            )
        opened = self.open()
        assert opened is self
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> "Trunk":
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        result = self.close()
        if asyncio.iscoroutine(result):
            await result

    def _ensure_open(self) -> None:
        if self._engine is not None:
            return
        with self._open_lock:
            if self._engine is not None:
                return
            if self._closed:
                raise RuntimeError("Trunk: cannot reuse a closed instance; create a new Trunk()")
            repository, backend = self._resolve_repository_and_backend()
            if self._branch is not None:
                repository.set_current_branch(self._branch)
            self._repository = repository
            self._backend = backend
            self._engine = Engine(repository, backend)

    def _resolve_repository_and_backend(self) -> tuple[Repository, Backend | None]:
        if isinstance(self._backend_arg, Backend):
            repository = self._repository_for_cwd()
            return repository, self._backend_arg

        if self._backend_arg == "memory" or self._backend_arg == "memory://":
            self._tmpdir = tempfile.TemporaryDirectory(prefix="trunks-memory-")
            repository = Repository.init(self._tmpdir.name, name=self._name or "memory", backend="memory://")
            return repository, backend_from_url("memory://")

        if isinstance(self._backend_arg, str):
            if self._looks_like_local_db(self._backend_arg):
                path = Path(self._backend_arg)
                repository = Repository(path)
                if not path.exists():
                    repository._initialize(RepoConfig(self._name or path.stem))
                return repository, None
            repository = self._repository_for_cwd()
            repository.set_backend_url(resolve_backend_url(self._backend_arg, repository.name))
            return repository, self._backend_for_repository(repository)

        repository = self._repository_for_cwd()
        return repository, self._backend_for_repository(repository)

    def _repository_for_cwd(self) -> Repository:
        try:
            return Repository.find()
        except RepositoryNotFound:
            config = GlobalConfig.load()
            repo_name = repo_name_from_env(self._name or Path.cwd().name or "repo")
            backend = backend_from_env(repo_name)
            if backend is None and not env_forces_local_only():
                backend = resolve_backend_url(config.default_backend, repo_name)
            repo = Repository.init(name=repo_name, backend=backend)
            env_mirrors = mirror_backends_from_env(repo_name)
            if env_mirrors:
                repo.set_mirror_urls(env_mirrors)
            return repo

    def _backend_for_repository(self, repository: Repository) -> Backend | None:
        if env_forces_local_only():
            return None
        repo_name = repository.name
        env_backend_url = backend_from_env(repo_name)
        if env_backend_url:
            repository.set_backend_url(env_backend_url)
            primary = backend_from_url(env_backend_url)
        else:
            primary_profile = repository.primary_storage_profile()
            primary = (
                backend_from_storage(primary_profile, repo_name)
                if primary_profile is not None
                else backend_from_url(repository.backend_url())
            )
        mirror_profiles = repository.mirror_storage_profiles()
        mirrors = [backend_from_storage(profile, repo_name) for profile in mirror_profiles]
        mirrors.extend(backend_from_url(url) for url in mirror_backends_from_env(repo_name))
        mirrors = [backend for backend in mirrors if backend is not None]
        if primary is not None and mirrors:
            from .backends.multi import Multi

            return Multi(primary=primary, mirrors=mirrors)
        return primary

    def _looks_like_local_db(self, value: str) -> bool:
        return "://" not in value and (value.endswith(".trunk") or value.startswith(".trunks/"))
