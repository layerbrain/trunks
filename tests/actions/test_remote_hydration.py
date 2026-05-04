from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from trunks.actions.run import _collect_artifacts_from_workspace
from trunks.actions.hydration import REMOTE_WORKSPACE, materialize_commit, prepared_workspace
from trunks.repository import Repository
from trunks.sandboxes import (
    Event,
    ExecResult,
    HydrateProgress,
    SandboxFile,
    SandboxProviderInfo,
    Spec,
    TransferProgress,
)


class CapturingSandbox:
    def __init__(self) -> None:
        self.uploaded: dict[str, bytes] = {}

    async def hydrate(self) -> AsyncIterator[HydrateProgress]:
        yield HydrateProgress(bytes_total=0, bytes_done=0, objects_total=0, objects_done=0)

    async def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_s: int,
    ) -> AsyncIterator[Event]:
        yield ExecResult(exit_code=0, duration_ms=0, timed_out=False)

    async def upload(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            data = Path(file.source).read_bytes()
            self.uploaded[file.target] = data
            yield TransferProgress(file=file, bytes_total=len(data), bytes_done=len(data))

    async def download(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        if False:
            yield

    async def cancel(self) -> None:
        return None

    async def destroy(self) -> None:
        return None


class PartialArtifactSandbox(CapturingSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.remote_files = {
            f"{REMOTE_WORKSPACE}/logs/app.log": b"app-log\n",
            f"{REMOTE_WORKSPACE}/logs/tunnel.log": b"tunnel-log\n",
        }

    async def download(self, files: tuple[SandboxFile, ...]) -> AsyncIterator[TransferProgress]:
        for file in files:
            data = self.remote_files.get(file.source)
            if data is None:
                raise FileNotFoundError(file.source)
            target = Path(file.target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            yield TransferProgress(file=file, bytes_total=len(data), bytes_done=len(data))


def _remote_info() -> SandboxProviderInfo:
    return SandboxProviderInfo(
        id="remote-test",
        capabilities=frozenset({"linux", "remote"}),
        isolation=frozenset({"container"}),
        architectures=frozenset({"x86_64"}),
        network_modes=frozenset({"default"}),
        gpu_kinds=frozenset(),
        max_cpu=8,
        max_memory_gib=16,
        max_disk_gib=100,
        max_timeout_s=3600,
        specs=(Spec(cpu=1, memory_gib=1, disk_gib=1),),
        regions=frozenset({"test"}),
        secrets_passthrough=True,
        closure_fetch=True,
        artifact_upload=True,
        live_logs=False,
    )


class RemoteHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worktree_hydration_uploads_only_trackable_files_to_remote_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            (root / "src").mkdir()
            (root / "src" / "app.txt").write_text("app\n", encoding="utf-8")
            (root / ".trunksignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
            sandbox = CapturingSandbox()

            async with prepared_workspace(repo, sandbox, _remote_info(), commit="worktree", cwd=str(root)) as cwd:
                self.assertEqual(cwd, REMOTE_WORKSPACE)

            self.assertEqual(sandbox.uploaded[f"{REMOTE_WORKSPACE}/src/app.txt"], b"app\n")
            self.assertNotIn(f"{REMOTE_WORKSPACE}/ignored.txt", sandbox.uploaded)

    async def test_commit_hydration_materializes_requested_commit_not_current_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            (root / "value.txt").write_text("first\n", encoding="utf-8")
            repo.add_all_worktree_files()
            first = repo.create_commit(message="first")
            (root / "value.txt").write_text("second\n", encoding="utf-8")
            repo.add_all_worktree_files()
            repo.create_commit(message="second")
            sandbox = CapturingSandbox()

            async with prepared_workspace(repo, sandbox, _remote_info(), commit=str(first.id), cwd=str(root)) as cwd:
                self.assertEqual(cwd, REMOTE_WORKSPACE)

            self.assertEqual(sandbox.uploaded[f"{REMOTE_WORKSPACE}/value.txt"], b"first\n")

    def test_materialize_commit_reconstructs_nested_tree_from_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            (root / "pkg").mkdir()
            (root / "pkg" / "mod.py").write_text("print('ok')\n", encoding="utf-8")
            repo.add_all_worktree_files()
            commit = repo.create_commit(message="nested")
            target = root / "checkout"

            materialize_commit(repo, str(commit.id), target)

            self.assertEqual((target / "pkg" / "mod.py").read_text(encoding="utf-8"), "print('ok')\n")

    async def test_remote_artifact_collection_keeps_available_files_when_one_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            sandbox = PartialArtifactSandbox()

            artifacts = await _collect_artifacts_from_workspace(
                repo,
                "run",
                sandbox=sandbox,
                provider=_remote_info(),
                root=REMOTE_WORKSPACE,
                paths=("report.json", "logs/app.log", "logs/tunnel.log"),
            )

            self.assertEqual([artifact["name"] for artifact in artifacts], ["logs/app.log", "logs/tunnel.log"])


if __name__ == "__main__":
    unittest.main()
