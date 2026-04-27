from __future__ import annotations

from .backend import Backend
from .ids import ObjectId
from .objects import parse_canonical_object, parse_commit_payload, parse_tree_payload
from .repository import Repository


class Fetch:
    def __init__(self, repository: Repository, backend: Backend) -> None:
        self.repository = repository
        self.backend = backend

    async def run(self) -> None:
        async for ref in self.backend.list_refs("refs/"):
            if ref.oid is None:
                continue
            await self._download_closure(ref.oid)
            self.repository.set_ref(ref.name, ref.oid)
            self.repository.set_backend_ref(ref.name, ref.oid)

    async def _download_closure(self, oid: ObjectId, seen: set[ObjectId] | None = None) -> None:
        seen = seen or set()
        if oid in seen:
            return
        seen.add(oid)
        try:
            self.repository.object_data(oid)
            return
        except Exception:
            pass
        data = await self.backend.read_object(oid)
        self.repository.put_object(oid, data)
        kind, payload = parse_canonical_object(data)
        if kind == "commit":
            commit = parse_commit_payload(payload, oid)
            await self._download_closure(commit.tree, seen)
            for parent in commit.parents:
                await self._download_closure(parent, seen)
        elif kind == "tree":
            tree = parse_tree_payload(payload)
            for entry in tree.entries:
                await self._download_closure(entry.oid, seen)

