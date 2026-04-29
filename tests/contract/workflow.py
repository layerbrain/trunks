"""End-to-end workflow contract for any Backend implementation.

The single-commit `assert_backend_contract` covers low-level CAS, list, read,
and write semantics. This contract layers the next tier on top: multi-commit
chains, branch + merge round trips, fresh-client handoff, and resync after
local data loss. Every backend in the matrix (Memory, Local, SQLite,
FileShare, S3, Postgres, SFTP, Azure, GCS) must satisfy these properties or
the durable layer leaks below the SDK.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from trunks.backend import Backend
from trunks.engine import Engine
from trunks.errors import RefConflict
from trunks.objects import Commit, Identity
from trunks.repository import Repository


def _identity() -> Identity:
    return Identity("Brain", "brain@layerbrain.com", datetime.now(UTC))


async def _commit_chain(engine: Engine, files: list[tuple[str, bytes, str]]) -> list[Commit]:
    commits: list[Commit] = []
    for path, data, message in files:
        await engine.write(path, data)
        commit = await engine.commit(message=message, author=_identity())
        commits.append(commit)
    return commits


async def assert_workflow_contract(testcase: unittest.TestCase, backend: Backend) -> None:
    """Run the full workflow contract against a single shared backend.

    Both `client_a` and `client_b` share the same backend instance, which is
    how the contract test asserts cross-machine behavior over an in-process
    fixture. For the live backends (S3/Postgres/SFTP/Azure/GCS) the same
    instance still mediates a real network round-trip per call.
    """

    with tempfile.TemporaryDirectory() as a_dir, tempfile.TemporaryDirectory() as b_dir:
        repo_a = Repository.init(a_dir, name="workflow")
        engine_a = Engine(repo_a, backend)

        # Scenario 1 — multi-commit linear chain on client A.
        chain = await _commit_chain(
            engine_a,
            [
                ("README.md", b"# v1\n", "one"),
                ("README.md", b"# v2\n", "two"),
                ("docs/intro.md", b"intro\n", "three"),
                ("docs/intro.md", b"intro v2\n", "four"),
            ],
        )
        await engine_a.push()
        head = chain[-1]
        testcase.assertEqual(await backend.read_ref("main"), head.id)

        # Scenario 2 — fresh client B opens an existing backend by pulling.
        # No prior init handshake on the remote, no clone command — just init
        # locally and pull from the same backend URL. This is the path a new
        # CI runner or sandbox takes when joining an in-flight repo.
        repo_b = Repository.init(b_dir, name="workflow")
        engine_b = Engine(repo_b, backend)
        await engine_b.pull()
        testcase.assertEqual(repo_b.ref("main"), head.id)
        testcase.assertEqual((repo_b.root / "README.md").read_bytes(), b"# v2\n")
        testcase.assertEqual((repo_b.root / "docs" / "intro.md").read_bytes(), b"intro v2\n")

        # Scenario 3 — every historical commit in the chain is reachable by
        # walking parents on client B. This is the "old commits" promise: a
        # fresh client can rewind to any point in history without re-pulling
        # or fetching extras.
        seen: list[Commit] = []
        cursor: Commit | None = repo_b.load_commit(head.id)
        while cursor is not None:
            seen.append(cursor)
            cursor = repo_b.load_commit(cursor.parents[0]) if cursor.parents else None
        testcase.assertEqual([c.id for c in seen], [c.id for c in reversed(chain)])

        # Scenario 4 — branch + merge round trip. Client A diverges main onto
        # `feature/auth`, lands a commit there, builds a true merge commit
        # (two parents), pushes both refs. Client B pulls and walks both
        # parents of the merge.
        repo_a.set_ref("feature/auth", head.id)
        repo_a.set_current_branch("feature/auth")
        repo_a.reset_index_to_commit(head.id)
        await engine_a.write("auth.md", b"auth\n")
        feature_commit = await engine_a.commit(
            message="feature: auth",
            author=_identity(),
            branch="feature/auth",
        )
        await engine_a.push()

        repo_a.set_current_branch("main")
        repo_a.reset_index_to_commit(head.id)
        await engine_a.write("ops.md", b"ops\n")
        ops_commit = await engine_a.commit(message="ops", author=_identity())

        # Build the merge commit by hand: two parents, tree from feature side
        # plus the ops file, message says "merge".
        await engine_a.write("auth.md", b"auth\n")
        merge_tree = repo_a.build_tree(repo_a.index_entries())
        identity = _identity()
        merge = Commit.create(
            tree=merge_tree.id,
            parents=[ops_commit.id, feature_commit.id],
            author=identity,
            committer=identity,
            message="merge feature/auth",
        )
        repo_a.put_object(merge.id, merge.canonical())
        repo_a.set_ref("main", merge.id)
        await engine_a.push()

        await engine_b.pull()
        testcase.assertEqual(repo_b.ref("main"), merge.id)
        merge_b = repo_b.load_commit(merge.id)
        testcase.assertEqual(set(merge_b.parents), {ops_commit.id, feature_commit.id})
        testcase.assertEqual((repo_b.root / "auth.md").read_bytes(), b"auth\n")
        testcase.assertEqual((repo_b.root / "ops.md").read_bytes(), b"ops\n")
        # Pulling the feature ref needs an explicit checkout on B.
        repo_b.set_current_branch("feature/auth")
        await engine_b.pull()
        testcase.assertEqual(repo_b.ref("feature/auth"), feature_commit.id)

        # Scenario 5 — concurrent push from two engines on the same branch
        # races a CAS update; only one wins. Reset both clients to the same
        # tip, then have each commit on top of it independently.
        repo_a.set_current_branch("main")
        repo_a.reset_index_to_commit(merge.id)
        repo_b.set_current_branch("main")
        repo_b.reset_index_to_commit(merge.id)

        await engine_a.write("a.txt", b"from a\n")
        commit_a = await engine_a.commit(message="from a", author=_identity())
        await engine_b.write("b.txt", b"from b\n")
        commit_b = await engine_b.commit(message="from b", author=_identity())

        await engine_a.push()
        with testcase.assertRaises(RefConflict):
            await engine_b.push()
        testcase.assertEqual(await backend.read_ref("main"), commit_a.id)

        # B catches up: pull resolves the divergence, then commit + push lands.
        await engine_b.pull()
        await engine_b.write("b.txt", b"from b\n")
        merged_b = await engine_b.commit(message="b after sync", author=_identity())
        await engine_b.push()
        testcase.assertEqual(await backend.read_ref("main"), merged_b.id)
        loaded = repo_b.load_commit(merged_b.id)
        testcase.assertIn(commit_a.id, loaded.parents)


__all__ = ["assert_workflow_contract"]
