from __future__ import annotations

from .backend import Backend
from .errors import ObjectNotFound
from .fetch import Fetch
from .repository import Repository


class Pull:
    def __init__(self, repository: Repository, backend: Backend) -> None:
        self.repository = repository
        self.backend = backend

    async def run(self) -> None:
        await Fetch(self.repository, self.backend).run()
        try:
            self.repository.apply_storage_config(await self.backend.read_config())
        except ObjectNotFound:
            pass
        head = self.repository.ref(self.repository.current_branch)
        if head is not None:
            old_entries = self.repository.index_entries()
            self.repository.reset_index_to_commit(head)
            self.repository.checkout_tree(head, previous_entries=old_entries)
