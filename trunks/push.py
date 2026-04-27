from __future__ import annotations

from dataclasses import dataclass

from .backend import Backend
from .errors import RefConflict
from .ids import ulid
from .journal import JournalEntry
from .repository import Repository
from .refs import normalize_ref


@dataclass(frozen=True)
class PushResult:
    refs_pushed: int
    refs_already_current: int
    objects_uploaded: int


class Push:
    def __init__(self, repository: Repository, backend: Backend) -> None:
        self.repository = repository
        self.backend = backend

    async def run(self) -> PushResult:
        objects = self.repository.all_objects()
        if self.repository.backend_url():
            await self.backend.write_config(self.repository.storage_config())
        refs_pushed = 0
        refs_current = 0
        current_oid = self.repository.ref(self.repository.current_branch)
        refs = (
            [(normalize_ref(self.repository.current_branch), current_oid)]
            if current_oid is not None
            else []
        )
        for ref_name, local_oid in refs:
            tx_id = ulid()
            expected = self.repository.backend_ref(ref_name)
            object_ids = [oid for oid, _ in objects]
            try:
                remote = await self.backend.read_ref(ref_name)
                if remote == local_oid:
                    await self.backend.write_objects(objects)
                    self.repository.set_backend_ref(ref_name, local_oid)
                    refs_current += 1
                    continue
                if remote is not None and remote != expected:
                    raise RefConflict(f"non-fast-forward: {ref_name}")
                expected = remote

                self.repository.record_push_transaction(
                    tx_id=tx_id,
                    source_ref=ref_name,
                    expected_remote_ref=expected,
                    result_ref=local_oid,
                    object_ids=object_ids,
                    status="pending",
                )
                await self.backend.write_objects(objects)
                self.repository.record_push_transaction(
                    tx_id=tx_id,
                    source_ref=ref_name,
                    expected_remote_ref=expected,
                    result_ref=local_oid,
                    object_ids=object_ids,
                    status="uploaded",
                )
                updated = await self.backend.cas_ref(ref_name, expected, local_oid)
                if not updated:
                    raise RefConflict(f"non-fast-forward: {ref_name}")
                self.repository.record_push_transaction(
                    tx_id=tx_id,
                    source_ref=ref_name,
                    expected_remote_ref=expected,
                    result_ref=local_oid,
                    object_ids=object_ids,
                    status="ref_updated",
                )
                journal = JournalEntry.create(
                    "push",
                    {
                        "tx_id": tx_id,
                        "ref": ref_name,
                        "expected": str(expected) if expected else None,
                        "new": str(local_oid),
                    },
                    id=tx_id,
                )
                await self.backend.append_journal(journal)
                self.repository.append_journal(journal)
                self.repository.set_backend_ref(ref_name, local_oid)
                self.repository.record_push_transaction(
                    tx_id=tx_id,
                    source_ref=ref_name,
                    expected_remote_ref=expected,
                    result_ref=local_oid,
                    object_ids=object_ids,
                    status="complete",
                )
                refs_pushed += 1
            except Exception:
                self.repository.record_push_transaction(
                    tx_id=tx_id,
                    source_ref=ref_name,
                    expected_remote_ref=expected,
                    result_ref=local_oid,
                    object_ids=object_ids,
                    status="failed",
                )
                raise
        return PushResult(
            refs_pushed=refs_pushed,
            refs_already_current=refs_current,
            objects_uploaded=len(objects) if (refs_pushed or refs_current) else 0,
        )
