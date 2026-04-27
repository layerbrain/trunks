from __future__ import annotations

import tempfile
import unittest
import sqlite3
import asyncio
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
import os

from trunks.backends.memory import Memory
from trunks.backends.local import Local
from trunks.backends.multi import Multi
from trunks.config import resolve_backend_url
from trunks.engine import Engine
from trunks.errors import BackendUnavailable, RefConflict
from trunks.ids import ObjectId
from trunks.repository import Repository
from trunks.trunk import Trunk


class FlakyMirror(Memory):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_write = True

    async def write_object(self, oid: ObjectId, data: bytes) -> None:
        if self.fail_next_write:
            self.fail_next_write = False
            raise BackendUnavailable("temporary mirror outage")
        await super().write_object(oid, data)


@contextmanager
def cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class PushPullTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_and_pull_between_repositories(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            repo_a = Repository.init(a)
            engine_a = Engine(repo_a, backend)
            await engine_a.write("README.md", b"# hi\n")
            commit = await engine_a.commit(message="init")
            await engine_a.push()
            conn = sqlite3.connect(repo_a.path)
            try:
                tx_id, status = conn.execute("select id, status from push_transactions").fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "complete")
            self.assertIn(tx_id, backend.journals)
            self.assertEqual(backend.journals[tx_id].data["tx_id"], tx_id)

            repo_b = Repository.init(b)
            engine_b = Engine(repo_b, backend)
            await engine_b.pull()

            self.assertEqual(repo_b.ref("main"), commit.id)
            self.assertEqual((repo_b.root / "README.md").read_bytes(), b"# hi\n")

    async def test_push_and_pull_through_local_backend_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as remote, tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            backend = Local(remote)
            repo_a = Repository.init(a)
            engine_a = Engine(repo_a, backend)
            await engine_a.write("README.md", b"# local\n")
            commit = await engine_a.commit(message="init")
            await engine_a.push()

            repo_b = Repository.init(b)
            engine_b = Engine(repo_b, backend)
            await engine_b.pull()

            self.assertEqual(repo_b.ref("main"), commit.id)
            self.assertEqual((repo_b.root / "README.md").read_bytes(), b"# local\n")

    async def test_push_reseeds_missing_remote_ref_when_backend_was_cleared(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            engine = Engine(repo, backend)
            await engine.write("README.md", b"# one\n")
            first = await engine.commit(message="one")
            await engine.push()
            self.assertEqual(repo.backend_ref("main"), first.id)

            backend.refs.clear()
            await engine.write("README.md", b"# two\n")
            second = await engine.commit(message="two")
            result = await engine.push()

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.refs_pushed, 1)
            self.assertEqual(await backend.read_ref("main"), second.id)
            self.assertEqual(repo.backend_ref("main"), second.id)

    async def test_push_only_sends_current_branch(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            engine = Engine(repo, backend)
            await engine.write("README.md", b"# main\n")
            main = await engine.commit(message="main")
            repo.create_branch("feature/a")
            await engine.write("feature.txt", b"feature\n")
            feature = await engine.commit(message="feature")

            result = await engine.push()

            self.assertIsNotNone(result)
            self.assertEqual(await backend.read_ref("feature/a"), feature.id)
            self.assertIsNone(await backend.read_ref("main"))
            self.assertEqual(repo.backend_ref("feature/a"), feature.id)
            self.assertIsNone(repo.backend_ref("main"))
            self.assertNotEqual(main.id, feature.id)

    async def test_concurrent_pushes_to_same_branch_have_one_winner(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            repo_a = Repository.init(a)
            engine_a = Engine(repo_a, backend)
            await engine_a.write("same.txt", b"agent-a\n")
            commit_a = await engine_a.commit(message="agent a")

            repo_b = Repository.init(b)
            engine_b = Engine(repo_b, backend)
            await engine_b.write("same.txt", b"agent-b\n")
            commit_b = await engine_b.commit(message="agent b")

            results = await asyncio.gather(engine_a.push(), engine_b.push(), return_exceptions=True)

            winners = [result for result in results if not isinstance(result, BaseException)]
            conflicts = [result for result in results if isinstance(result, RefConflict)]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(conflicts), 1)
            self.assertIn(await backend.read_ref("main"), {commit_a.id, commit_b.id})

    async def test_concurrent_pushes_to_different_branches_both_succeed(self) -> None:
        backend = Memory()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            repo_a = Repository.init(a)
            engine_a = Engine(repo_a, backend)
            await engine_a.write("a.txt", b"a\n")
            commit_a = await engine_a.commit(message="a")

            repo_b = Repository.init(b)
            repo_b.set_current_branch("feature/b")
            engine_b = Engine(repo_b, backend)
            await engine_b.write("b.txt", b"b\n")
            commit_b = await engine_b.commit(message="b")

            await asyncio.gather(engine_a.push(), engine_b.push())

            self.assertEqual(await backend.read_ref("main"), commit_a.id)
            self.assertEqual(await backend.read_ref("feature/b"), commit_b.id)

    async def test_env_repo_config_pushes_without_home_config(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as remote_dir:
            root = Path(root_dir)
            remote = Path(remote_dir)
            with cwd(root), patch.dict(os.environ, {
                "TRUNKS_REPO": f"file://{remote / 'repo.trunk'}",
                "TRUNKS_REPO_NAME": "sandbox-repo",
            }, clear=False):
                async with Trunk() as trunk:
                    await trunk.write("README.md", b"# sandbox\n")
                    commit = await trunk.commit(message="init")
                    await trunk.push()
                    self.assertEqual(trunk.repository.name, "sandbox-repo")
                    self.assertEqual(trunk.repository.backend_url(), f"file://{remote / 'repo.trunk'}")

            pulled = Repository.init(Path(tempfile.mkdtemp()))
            await Engine(pulled, Local(remote / "repo.trunk")).pull()
            self.assertEqual(pulled.ref("main"), commit.id)

    async def test_env_backend_base_resolves_repo_name_and_mirrors(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_dir,
            tempfile.TemporaryDirectory() as primary_dir,
            tempfile.TemporaryDirectory() as mirror_dir,
        ):
            root = Path(root_dir)
            primary = Path(primary_dir)
            mirror = Path(mirror_dir)
            with cwd(root), patch.dict(os.environ, {
                "TRUNKS_BACKEND": f"file://{primary}",
                "TRUNKS_MIRRORS": f"file://{mirror}",
                "TRUNKS_REPO_NAME": "agent-sandbox",
            }, clear=False):
                async with Trunk() as trunk:
                    await trunk.write("README.md", b"# mirrored\n")
                    commit = await trunk.commit(message="init")
                    await trunk.push()
                    expected_primary = f"file://{primary}/trunks/agent-sandbox.trunk"
                    self.assertEqual(trunk.repository.backend_url(), expected_primary)
                    self.assertEqual(trunk.repository.mirror_urls(), [f"file://{mirror}/trunks/agent-sandbox.trunk"])

            self.assertEqual((primary / "trunks" / "agent-sandbox.trunk" / "refs" / "heads" / "main").read_text().strip(), str(commit.id))
            self.assertEqual((mirror / "trunks" / "agent-sandbox.trunk" / "refs" / "heads" / "main").read_text().strip(), str(commit.id))

    async def test_pull_learns_storage_map_for_new_sandbox(self) -> None:
        with (
            tempfile.TemporaryDirectory() as primary_dir,
            tempfile.TemporaryDirectory() as mirror_dir,
            tempfile.TemporaryDirectory() as agent_a_dir,
            tempfile.TemporaryDirectory() as agent_b_dir,
        ):
            primary_root = Path(primary_dir) / "trunks" / "demo.trunk"
            mirror_root = Path(mirror_dir) / "trunks" / "demo.trunk"
            primary_url = f"local://{primary_root}"
            mirror_url = f"local://{mirror_root}"

            repo_a = Repository.init(agent_a_dir, name="demo")
            repo_a.set_primary_storage("primary", primary_url)
            repo_a.set_mirror_storage("backup", mirror_url)
            engine_a = Engine(repo_a, Multi(primary=Local(primary_root), mirrors=[Local(mirror_root)]))
            await engine_a.write("README.md", b"# demo\n")
            await engine_a.commit(message="init")
            await engine_a.push()

            repo_b = Repository.init(agent_b_dir, name="demo", backend=primary_url)
            await Engine(repo_b, Local(primary_root)).pull()

            self.assertEqual(repo_b.backend_url(), primary_url)
            self.assertEqual(repo_b.primary_storage_name(), "primary")
            self.assertEqual(repo_b.mirror_targets(), [("backup", mirror_url)])

    async def test_strict_mirror_failure_does_not_advance_primary_ref(self) -> None:
        primary = Memory()
        mirror = FlakyMirror()
        backend = Multi(primary=primary, mirrors=[mirror])
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            engine = Engine(repo, backend)
            await engine.write("README.md", b"# retry\n")
            commit = await engine.commit(message="init")

            with self.assertRaises(BackendUnavailable):
                await engine.push()
            self.assertIsNone(await primary.read_ref("main"))
            self.assertLess(len(mirror.objects), len(primary.objects))

            await engine.push()

            self.assertEqual(await primary.read_ref("main"), commit.id)
            self.assertEqual(await mirror.read_ref("main"), commit.id)
            self.assertEqual(await mirror.read_object(commit.id), repo.object_data(commit.id))
            self.assertEqual(set(mirror.objects), set(primary.objects))

    async def test_local_only_env_forces_no_backend(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as remote_dir:
            root = Path(root_dir)
            with cwd(root), patch.dict(os.environ, {
                "TRUNKS_LOCAL_ONLY": "1",
                "TRUNKS_REPO": f"file://{Path(remote_dir) / 'repo.trunk'}",
            }, clear=False):
                async with Trunk() as trunk:
                    await trunk.write("README.md", b"# local\n")
                    await trunk.commit(message="init")
                    await trunk.push()
                    self.assertIsNone(trunk.repository.backend_url())

    def test_default_backend_resolution(self) -> None:
        self.assertEqual(
            resolve_backend_url("s3://company", "lazy-lms"),
            "s3://company/trunks/lazy-lms.trunk",
        )
        self.assertEqual(
            resolve_backend_url("s3://company/{repo}.trunk", "lazy-lms"),
            "s3://company/lazy-lms.trunk",
        )


if __name__ == "__main__":
    unittest.main()
