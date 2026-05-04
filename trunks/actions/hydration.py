from __future__ import annotations

import tempfile
import shlex
import tarfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from trunks.ids import ObjectId
from trunks.objects import parse_canonical_object, parse_commit_payload, parse_tree_payload
from trunks.paths import is_ignored, is_internal_path
from trunks.repository import Repository
from trunks.sandboxes import ExecResult, Sandbox, SandboxFile, SandboxProviderInfo, TransferProgress

from .backend_store import store

REMOTE_WORKSPACE = "/workspace"


@asynccontextmanager
async def prepared_workspace(
    repo: Repository | None,
    sandbox: Sandbox,
    provider: SandboxProviderInfo,
    *,
    commit: str,
    cwd: str | None,
) -> AsyncIterator[str]:
    local_cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd().resolve())
    if repo is None:
        yield REMOTE_WORKSPACE if "remote" in provider.capabilities else local_cwd
        return

    if commit == "worktree" and "remote" not in provider.capabilities:
        yield str(Path(cwd or repo.root).resolve())
        return

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    source = Path(cwd or repo.root).resolve()
    try:
        if commit != "worktree":
            temp_dir = tempfile.TemporaryDirectory(prefix="trunks-actions-workspace-")
            source = Path(temp_dir.name)
            materialize_commit(repo, commit, source)
        if "remote" in provider.capabilities:
            target_root = await _remote_workspace_root(sandbox)
            async for _progress in upload_workspace(sandbox, source, target_root=target_root):
                pass
            yield target_root
        else:
            yield str(source)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


async def upload_workspace(
    sandbox: Sandbox,
    source: Path,
    *,
    target_root: str = REMOTE_WORKSPACE,
) -> AsyncIterator[TransferProgress]:
    source = source.resolve()
    source_files = _worktree_files(source)
    if len(source_files) >= 16:
        async for progress in _upload_workspace_archive(sandbox, source, source_files, target_root=target_root):
            yield progress
        return
    files = tuple(SandboxFile(source=str(path), target=f"{target_root.rstrip('/')}/{path.relative_to(source).as_posix()}") for path in source_files)
    if not files:
        return
    async for progress in sandbox.upload(files):
        yield progress


async def _upload_workspace_archive(
    sandbox: Sandbox,
    source: Path,
    files: list[Path],
    *,
    target_root: str,
) -> AsyncIterator[TransferProgress]:
    with tempfile.TemporaryDirectory(prefix="trunks-actions-upload-") as tmp:
        archive = Path(tmp) / "workspace.tar"
        with tarfile.open(archive, "w") as tar:
            for path in files:
                tar.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
        remote_archive = f"/tmp/trunks-workspace-{uuid.uuid4().hex}.tar"
        async for progress in sandbox.upload((SandboxFile(source=str(archive), target=remote_archive),)):
            yield progress
        command = (
            f"mkdir -p {shlex.quote(target_root)} && "
            f"tar -xf {shlex.quote(remote_archive)} -C {shlex.quote(target_root)} && "
            f"rm -f {shlex.quote(remote_archive)}"
        )
        result: ExecResult | None = None
        async for event in sandbox.run(["/bin/sh", "-lc", command], {}, target_root, 300):
            if isinstance(event, ExecResult):
                result = event
        if result is None or result.exit_code != 0:
            raise RuntimeError("remote workspace archive extraction failed")


def materialize_commit(repo: Repository, commit: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    oid = ObjectId(commit)
    kind, payload = parse_canonical_object(store(repo).read_object(oid))
    if kind != "commit":
        raise ValueError(f"expected commit object for {commit}, got {kind}")
    parsed = parse_commit_payload(payload, oid)
    _materialize_tree(repo, parsed.tree, target)


def _materialize_tree(repo: Repository, tree: ObjectId, target: Path) -> None:
    kind, payload = parse_canonical_object(store(repo).read_object(tree))
    if kind != "tree":
        raise ValueError(f"expected tree object for {tree}, got {kind}")
    for entry in parse_tree_payload(payload).entries:
        entry_path = (target / entry.name).resolve(strict=False)
        if not entry_path.is_relative_to(target.resolve(strict=False)):
            raise ValueError(f"tree entry escapes workspace: {entry.name}")
        if entry.mode == "40000":
            entry_path.mkdir(parents=True, exist_ok=True)
            _materialize_tree(repo, entry.oid, entry_path)
            continue
        blob_kind, blob_payload = parse_canonical_object(store(repo).read_object(entry.oid))
        if blob_kind != "blob":
            raise ValueError(f"expected blob object for {entry.oid}, got {blob_kind}")
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        entry_path.write_bytes(blob_payload)
        if entry.mode == "100755":
            entry_path.chmod(0o755)


def _worktree_files(root: Path) -> list[Path]:
    root = root.resolve()
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.resolve(strict=False).relative_to(root).as_posix()
        if is_internal_path(relative) or is_ignored(relative, root):
            continue
        files.append(path)
    return files


async def _remote_workspace_root(sandbox: Sandbox) -> str:
    workspace_root = getattr(sandbox, "workspace_root", None)
    if workspace_root is None:
        return REMOTE_WORKSPACE
    value = workspace_root()
    if hasattr(value, "__await__"):
        value = await value
    return str(value or REMOTE_WORKSPACE)
