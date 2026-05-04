from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import AsyncIterator

from .backend import Backend
from .errors import BackendUnavailable
from .ids import ObjectId
from .objects import Commit, Identity
from .repository import Repository, Status


class Engine:
    def __init__(self, repository: Repository, backend: Backend | None = None) -> None:
        self.repository = repository
        self.backend = backend
        self._backend_entered = False

    async def open(self) -> None:
        if self.backend is not None and not self._backend_entered:
            await self.backend.__aenter__()
            self._backend_entered = True

    async def close(self) -> None:
        if self.backend is not None and self._backend_entered:
            await self.backend.__aexit__(None, None, None)
            self._backend_entered = False

    async def write(self, path: str, data: bytes, *, branch: str | None = None) -> None:
        self.repository.write_file(path, data, branch=branch)

    async def read(self, path: str, *, branch: str | None = None) -> bytes:
        return self.repository.read_file(path, branch=branch)

    async def list(self, path: str = "", *, branch: str | None = None) -> list[str]:
        return self.repository.list_dir(path, branch=branch)

    async def exists(self, path: str, *, branch: str | None = None) -> bool:
        return self.repository.exists(path, branch=branch)

    async def delete(self, path: str, *, branch: str | None = None) -> None:
        self.repository.delete_file(path, branch=branch)

    async def copy(self, source: str, dest: str, *, branch: str | None = None) -> None:
        self.repository.copy_file(source, dest, branch=branch)

    async def move(self, source: str, dest: str, *, branch: str | None = None) -> None:
        self.repository.move_file(source, dest, branch=branch)

    async def mkdir(self, path: str) -> None:
        self.repository.make_dir(path)

    async def commit(
        self,
        *,
        message: str,
        author: Identity | None = None,
        committer: Identity | None = None,
        branch: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Commit:
        return self.repository.create_commit(
            message=message,
            author=author,
            committer=committer,
            branch=branch,
            labels=labels,
        )

    async def status(self) -> Status:
        return self.repository.status()

    async def fetch(self) -> None:
        from .fetch import Fetch

        if self.backend is None:
            return
        await self.open()
        await Fetch(self.repository, self.backend).run()

    async def pull(self) -> None:
        from . import audit
        from .pull import Pull

        if self.backend is None:
            return
        await self.open()
        await Pull(self.repository, self.backend).run()
        audit.record(
            self.repository,
            "pull",
            {
                "branch": self.repository.current_branch,
                "head": str(self.repository.ref(self.repository.current_branch)),
            },
        )

    async def push(self) -> "PushResult | None":
        from .push import Push

        if self.backend is None:
            return None
        await self.open()
        mirror_failures = getattr(self.backend, "mirror_failures", None)
        if mirror_failures is not None:
            mirror_failures.clear()
        result = await Push(self.repository, self.backend).run()
        mirror_failures = getattr(self.backend, "mirror_failures", [])
        if mirror_failures:
            raise BackendUnavailable("mirror sync failed: " + "; ".join(mirror_failures))
        from . import audit, webhooks

        push_payload = {
            "branch": self.repository.current_branch,
            "head": str(self.repository.ref(self.repository.current_branch)),
            "refs_pushed": result.refs_pushed,
            "objects_uploaded": result.objects_uploaded,
        }
        audit.record(self.repository, "push", push_payload)
        await webhooks.emit(self.repository, "push", push_payload)
        if result.refs_pushed:
            from .actions.triggers import run_push_workflows

            workflow_runs = await run_push_workflows(self.repository, commit=str(push_payload["head"]), branch=push_payload["branch"])
            if workflow_runs:
                _spawn_executor(self.repository, workflow_runs)
        return result

    async def log(self, *, branch: str | None = None, limit: int = 50) -> AsyncIterator[Commit]:
        current = self.repository.ref(branch or self.repository.current_branch)
        count = 0
        while current is not None and count < limit:
            commit = self.repository.load_commit(current)
            yield commit
            count += 1
            current = commit.parents[0] if commit.parents else None

    async def log_list(self, *, branch: str | None = None, limit: int = 50) -> list[Commit]:
        return [commit async for commit in self.log(branch=branch, limit=limit)]


def _spawn_executor(repo: Repository, workflow_runs: list[dict[str, object]]) -> None:
    run_ids = []
    for wf_run in workflow_runs:
        for job in wf_run.get("jobs", []):
            run_id = job.get("run")
            if run_id and job.get("phase") == "pending":
                run_ids.append(str(run_id))
    if not run_ids:
        return
    env = os.environ.copy()
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_parent}{os.pathsep}{existing}" if existing else package_parent
    for run_id in run_ids:
        subprocess.Popen(
            [sys.executable, "-m", "trunks.cli", "actions", "execute", "--run-id", run_id],
            cwd=str(repo.root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
