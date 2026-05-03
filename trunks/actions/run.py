from __future__ import annotations

import os
import shlex
import sys
import contextlib
import tempfile
from collections.abc import Awaitable, Mapping
from pathlib import Path

from trunks._auto import run_auto
from trunks.errors import RepositoryNotFound
from trunks.ids import ObjectId, ulid
from trunks.objects import parse_canonical_object, parse_commit_payload, parse_tree_payload
from trunks.repository import Repository
from trunks.sandboxes import ExecResult, Isolation, LogChunk, SandboxFile, SandboxRequest, SandboxProviderInfo, Spec
from trunks.sandboxes.registry import ProviderRegistry

from .artifacts import collect_artifacts
from .backend_store import store
from .hydration import prepared_workspace
from .secrets import mask_log_chunk, masking_env, resolve_secret_env
from .state import Run, RunState
from .storage import persist_run
from .status_writeback import github_checks_writeback_from_env_async


def run_command(
    command: str | list[str],
    *,
    commit: str = "worktree",
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: int = 30 * 60,
    provider_id: str | None = None,
    strict_provider: bool = False,
    region: str | None = None,
    spec: Spec | None = None,
    artifact_paths: tuple[str, ...] = (),
    isolation: Isolation = "process",
) -> Run | Awaitable[Run]:
    return run_auto(
        lambda: run_command_async(
            command,
            commit=commit,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            provider_id=provider_id,
            strict_provider=strict_provider,
            region=region,
            spec=spec,
            artifact_paths=artifact_paths,
            isolation=isolation,
        )
    )


async def run_command_async(
    command: str | list[str],
    *,
    commit: str = "worktree",
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: int = 30 * 60,
    provider_id: str | None = None,
    strict_provider: bool = False,
    region: str | None = None,
    spec: Spec | None = None,
    artifact_paths: tuple[str, ...] = (),
    isolation: Isolation = "process",
) -> Run:
    try:
        repo = Repository.find(cwd)
    except RepositoryNotFound:
        repo = None
    secret_env = {}
    if repo is not None:
        secret_env.update(resolve_secret_env(repo))
    run_env = dict(secret_env)
    run_env.update(env or {})
    log_mask_env = masking_env(secret_env, env)
    resolved_spec = spec or Spec(cpu=1, memory_gib=1, disk_gib=1, arch=_host_arch())
    registry = await ProviderRegistry.discover()
    provider = await registry.resolve(
        capabilities=_capabilities_for_isolation(isolation),
        spec=resolved_spec,
        region=region,
        isolation=isolation,
        network=resolved_spec.network,
        provider=provider_id,
        strict_provider=strict_provider,
    )
    run = ulid()
    request = SandboxRequest(
        run=run,
        commit=commit,
        spec=resolved_spec,
        region=region or sorted(provider.info.regions)[0],
        isolation=isolation,
        network=resolved_spec.network,
        timeout_s=timeout_s,
        artifacts=artifact_paths,
    )
    sandbox = await provider.create(request)
    logs: list[LogChunk] = []
    result: ExecResult | None = None
    artifacts: tuple[dict[str, object], ...] = ()
    try:
        async for _progress in sandbox.hydrate():
            pass
        argv = _command_argv(command)
        async with prepared_workspace(repo, sandbox, provider.info, commit=commit, cwd=cwd) as sandbox_cwd:
            async for event in sandbox.run(argv, run_env, sandbox_cwd, timeout_s):
                if isinstance(event, LogChunk):
                    logs.append(mask_log_chunk(event, log_mask_env))
                else:
                    result = event
            if repo is not None and artifact_paths:
                artifacts = await _collect_artifacts_from_workspace(
                    repo,
                    run,
                    sandbox=sandbox,
                    provider=provider.info,
                    root=sandbox_cwd,
                    paths=artifact_paths,
                )
    finally:
        await sandbox.destroy()
    phase = "succeeded" if result is not None and result.exit_code == 0 and not result.timed_out else "failed"
    state = RunState(
        phase=phase,
        token=None,
        expires_at=None,
        result_oid=None,
        attempt=1,
        executor="inline",
        provider=provider.info.id,
        spec_key=resolved_spec.key,
        requested_region=region,
        region=request.region,
    )
    artifacts = artifacts if repo is not None else ()
    action_run = Run(
        id=run,
        commit=commit,
        command=tuple(_command_argv(command)),
        spec=resolved_spec,
        timeout_s=timeout_s,
        isolation=isolation,
        provider_id=provider_id,
        strict_provider=strict_provider,
        artifact_paths=artifact_paths,
        state=state,
        logs=tuple(logs),
        result=result,
        artifacts=artifacts,
    )
    if repo is None:
        return action_run
    persist_run(repo, action_run)
    with contextlib.suppress(Exception):
        await github_checks_writeback_from_env_async(action_run.to_dict())
    return action_run


async def _collect_artifacts_from_workspace(
    repo: Repository,
    run: str,
    *,
    sandbox: object,
    provider: SandboxProviderInfo,
    root: str,
    paths: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    if "remote" not in provider.capabilities:
        return collect_artifacts(repo, run, root=root, paths=paths)
    with tempfile.TemporaryDirectory(prefix="trunks-actions-artifacts-") as tmp:
        local_root = Path(tmp)
        downloads = tuple(
            SandboxFile(
                source=path if path.startswith("/") else f"{root.rstrip('/')}/{path}",
                target=str(local_root / path.lstrip("/")),
            )
            for path in paths
        )
        with contextlib.suppress(Exception):
            async for _progress in sandbox.download(downloads):  # type: ignore[attr-defined]
                pass
        return collect_artifacts(repo, run, root=local_root, paths=paths)


def enqueue_command(
    repo: Repository,
    command: str | list[str],
    *,
    commit: str = "worktree",
    timeout_s: int = 30 * 60,
    provider_id: str | None = None,
    strict_provider: bool = False,
    region: str | None = None,
    spec: Spec | None = None,
    artifact_paths: tuple[str, ...] = (),
    isolation: Isolation = "process",
) -> Run:
    _ensure_commit_closure(repo, commit)
    resolved_spec = spec or Spec(cpu=1, memory_gib=1, disk_gib=1, arch=_host_arch())
    run = ulid()
    action_run = Run(
        id=run,
        commit=commit,
        command=tuple(_command_argv(command)),
        spec=resolved_spec,
        timeout_s=timeout_s,
        isolation=isolation,
        provider_id=provider_id,
        strict_provider=strict_provider,
        artifact_paths=artifact_paths,
        state=RunState(
            phase="pending",
            token=None,
            expires_at=None,
            result_oid=None,
            attempt=0,
            executor=None,
            provider=None,
            spec_key=resolved_spec.key,
            requested_region=region,
            region=None,
        ),
    )
    persist_run(repo, action_run)
    return action_run


def _ensure_commit_closure(repo: Repository, commit: str) -> None:
    if commit == "worktree":
        return
    try:
        root = ObjectId(commit)
    except ValueError as exc:
        raise ValueError(f"invalid commit oid for distributed action: {commit}") from exc
    action_store = store(repo)
    stack = [root]
    seen: set[ObjectId] = set()
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        try:
            kind, payload = parse_canonical_object(action_store.read_object(oid))
        except Exception as exc:
            raise ValueError(f"commit closure not in storage: missing {oid}") from exc
        if kind == "commit":
            parsed = parse_commit_payload(payload, oid)
            stack.append(parsed.tree)
            stack.extend(parsed.parents)
        elif kind == "tree":
            stack.extend(entry.oid for entry in parse_tree_payload(payload).entries)


def _command_argv(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        return command
    if os.name == "nt":
        return shlex.split(command)
    return ["/bin/sh", "-lc", command]


def _host_capability() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _capabilities_for_isolation(isolation: Isolation) -> frozenset[str]:
    if isolation == "container":
        return frozenset({"linux", "container"})
    if isolation in {"microvm", "vm"}:
        return frozenset({"linux"})
    return frozenset({_host_capability()})


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"
