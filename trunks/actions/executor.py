from __future__ import annotations

import asyncio
import contextlib
import tempfile
import os
import sys
import time
from collections.abc import Awaitable
from dataclasses import asdict
from json import JSONDecodeError
from pathlib import Path
from typing import cast

from trunks._auto import run_auto
from trunks.repository import Repository
from trunks.sandboxes import Arch, ExecResult, GPU, Isolation, LogChunk, NetworkMode, Sandbox, SandboxFile, SandboxRequest, SandboxProviderInfo, Spec
from trunks.sandboxes.registry import ProviderRegistry

from .artifacts import collect_artifacts
from .capacity import claim_capacity_slot, heartbeat_capacity_slot, release_capacity_slot
from .health import degraded_provider_ids, record_provider_failure, record_provider_success
from .hydration import prepared_workspace
from .secrets import mask_log_chunk, mask_secret_text, resolve_secret_env
from .status_writeback import github_checks_writeback_from_env_async
from .storage import append_run_logs, claim_run, complete_run, heartbeat_run, load_run, pending_run_ids, release_run_claim


def execute_once(
    repo: Repository,
    *,
    executor: str = "executor",
    provider_id: str | None = None,
    region: str | None = None,
    run_id: str | None = None,
    cwd: str | None = None,
    lease_ttl_s: int = 90,
    heartbeat_interval_s: int = 30,
) -> dict[str, object] | None | Awaitable[dict[str, object] | None]:
    return run_auto(
        lambda: execute_once_async(
            repo,
            executor=executor,
            provider_id=provider_id,
            region=region,
            run_id=run_id,
            cwd=cwd,
            lease_ttl_s=lease_ttl_s,
            heartbeat_interval_s=heartbeat_interval_s,
        )
    )


async def execute_once_async(
    repo: Repository,
    *,
    executor: str = "executor",
    provider_id: str | None = None,
    region: str | None = None,
    run_id: str | None = None,
    cwd: str | None = None,
    lease_ttl_s: int = 90,
    heartbeat_interval_s: int = 30,
) -> dict[str, object] | None:
    registry = await ProviderRegistry.discover()
    for run in ([run_id] if run_id else pending_run_ids(repo, limit=25)):
        if run is None:
            continue
        try:
            payload = load_run(repo, run)
            spec = _spec_from_payload(payload)
            isolation = _isolation_from_payload(payload)
            timeout_s = _timeout_from_payload(payload)
        except (KeyError, TypeError, ValueError, JSONDecodeError):
            continue
        requested_region = region or _requested_region(payload)
        requested_provider = provider_id or _provider_from_payload(payload)
        strict_provider = provider_id is not None or _strict_provider_from_payload(payload)
        try:
            chain = await registry.resolve_chain(
                capabilities=_capabilities_for_isolation(isolation),
                spec=spec,
                region=requested_region,
                isolation=isolation,
                network=spec.network,
                provider=requested_provider,
                strict_provider=strict_provider,
                avoid=degraded_provider_ids(repo, region=requested_region, spec_key=spec.key),
            )
        except KeyError:
            raise
        except LookupError:
            continue
        if not chain:
            continue
        provider = None
        sandbox: Sandbox | None = None
        capacity_claim: dict[str, object] | None = None
        claimed: dict[str, object] | None = None
        resolved_region: str | None = None
        token: str | None = None
        last_failure_reason: str | None = None
        boot_failures: list[tuple[str, str]] = []
        logs: list[LogChunk] = []
        for candidate in chain:
            candidate_region = requested_region or sorted(candidate.info.regions)[0]
            candidate_capacity = claim_capacity_slot(
                repo,
                region=candidate_region,
                spec_key=spec.key,
                owner=executor,
                ttl_s=lease_ttl_s,
            )
            if candidate_capacity is None:
                continue
            candidate_claim = claim_run(
                repo,
                run,
                executor=executor,
                provider=candidate.info.id,
                spec_key=spec.key,
                region=candidate_region,
                requested_region=requested_region,
                ttl_s=lease_ttl_s,
            )
            if candidate_claim is None:
                release_capacity_slot(repo, candidate_capacity)
                continue
            candidate_state = candidate_claim["state"]
            if not isinstance(candidate_state, dict) or not isinstance(candidate_state.get("token"), str):
                release_capacity_slot(repo, candidate_capacity)
                return None
            candidate_token = cast(str, candidate_state["token"])
            try:
                candidate_sandbox = await candidate.create(
                    SandboxRequest(
                        run=run,
                        commit=str(candidate_claim["commit"]),
                        spec=spec,
                        region=candidate_region,
                        isolation=isolation,
                        network=spec.network,
                        timeout_s=timeout_s,
                        artifacts=_artifact_paths_from_payload(candidate_claim),
                    )
                )
            except Exception as exc:
                last_failure_reason = mask_secret_text(
                    f"{exc.__class__.__name__}: {exc}", {}
                )
                boot_failures.append((candidate.info.id, last_failure_reason))
                record_provider_failure(
                    repo,
                    provider=candidate.info.id,
                    region=candidate_region,
                    spec_key=spec.key,
                    run=run,
                    reason=last_failure_reason,
                )
                logs.append(
                    LogChunk(
                        stream="stderr",
                        seq=len(logs) + 1,
                        text=f"provider {candidate.info.id} failed to boot: {last_failure_reason}\n",
                        ts_ms=int(time.time() * 1000),
                    )
                )
                release_run_claim(repo, candidate_claim, token=candidate_token)
                release_capacity_slot(repo, candidate_capacity)
                continue
            provider = candidate
            sandbox = candidate_sandbox
            capacity_claim = candidate_capacity
            claimed = candidate_claim
            resolved_region = candidate_region
            token = candidate_token
            break
        if provider is None or sandbox is None or claimed is None or token is None or capacity_claim is None or resolved_region is None:
            if not boot_failures:
                continue
            seal_region = requested_region or sorted(chain[0].info.regions)[0]
            seal_capacity = claim_capacity_slot(
                repo,
                region=seal_region,
                spec_key=spec.key,
                owner=executor,
                ttl_s=lease_ttl_s,
            )
            seal_claim = claim_run(
                repo,
                run,
                executor=executor,
                provider=chain[0].info.id,
                spec_key=spec.key,
                region=seal_region,
                requested_region=requested_region,
                ttl_s=lease_ttl_s,
            )
            if seal_claim is None:
                if seal_capacity is not None:
                    release_capacity_slot(repo, seal_capacity)
                continue
            seal_state = seal_claim["state"]
            if isinstance(seal_state, dict) and isinstance(seal_state.get("token"), str):
                seal_token = cast(str, seal_state["token"])
                reason = last_failure_reason or "no provider satisfied request"
                complete_run(
                    repo,
                    seal_claim,
                    token=seal_token,
                    phase="failed",
                    logs=[asdict(log) for log in logs] or [
                        asdict(
                            LogChunk(
                                stream="stderr",
                                seq=1,
                                text=f"provider failure: {reason}\n",
                                ts_ms=int(time.time() * 1000),
                            )
                        )
                    ],
                    result={"exit_code": 1, "duration_ms": 0, "timed_out": False, "provider_failure": True},
                )
            if seal_capacity is not None:
                release_capacity_slot(repo, seal_capacity)
            try:
                return load_run(repo, run)
            except KeyError:
                return None
        state = claimed["state"]
        secret_env: dict[str, str] = {}
        result: ExecResult | None = None
        completion_payload = claimed
        try:
            async for _progress in sandbox.hydrate():
                pass
            async with prepared_workspace(
                repo,
                sandbox,
                provider.info,
                commit=str(claimed["commit"]),
                cwd=cwd or str(repo.root),
            ) as sandbox_cwd:
                secret_env = resolve_secret_env(repo)
                aborted, result = await _run_sandbox_with_lease(
                    repo,
                    run=run,
                    sandbox=sandbox,
                    command=_command_from_payload(claimed),
                    cwd=sandbox_cwd,
                    env=secret_env,
                    mask_env=secret_env,
                    token=token,
                    capacity_claim=capacity_claim,
                    logs=logs,
                    lease_ttl_s=lease_ttl_s,
                    heartbeat_interval_s=heartbeat_interval_s,
                    timeout_s=timeout_s,
                )
                if not aborted:
                    completion_payload = await _attach_artifacts_from_workspace(
                        repo,
                        claimed,
                        sandbox=sandbox,
                        provider=provider.info,
                        sandbox_cwd=sandbox_cwd,
                    )
            if aborted:
                release_capacity_slot(repo, capacity_claim)
                try:
                    return load_run(repo, run)
                except KeyError:
                    return None
        except Exception as exc:
            reason = mask_secret_text(f"{exc.__class__.__name__}: {exc}", secret_env)
            record_provider_failure(
                repo,
                provider=provider.info.id,
                region=resolved_region,
                spec_key=spec.key,
                run=run,
                reason=reason,
            )
            logs.append(LogChunk(stream="stderr", seq=len(logs) + 1, text=f"provider failure: {reason}\n", ts_ms=int(time.time() * 1000)))
            complete_run(
                repo,
                claimed,
                token=token,
                phase="failed",
                logs=[asdict(log) for log in logs],
                result={"exit_code": 1, "duration_ms": 0, "timed_out": False, "provider_failure": True},
            )
            release_capacity_slot(repo, capacity_claim)
            try:
                return load_run(repo, run)
            except KeyError:
                return None
        finally:
            if sandbox is not None:
                with contextlib.suppress(Exception):
                    await sandbox.destroy()
        try:
            record_provider_success(repo, provider=provider.info.id, region=resolved_region, spec_key=spec.key, run=run)
        except Exception:
            pass
        try:
            phase = "succeeded" if result is not None and result.exit_code == 0 and not result.timed_out else "failed"
            complete_run(
                repo,
                completion_payload,
                token=token,
                phase=phase,
                logs=[asdict(log) for log in logs],
                result=None if result is None else asdict(result),
            )
            completed = load_run(repo, run)
            with contextlib.suppress(Exception):
                from .workflow import refresh_workflow_runs_for_action_run

                refresh_workflow_runs_for_action_run(repo, completed)
            with contextlib.suppress(Exception):
                await github_checks_writeback_from_env_async(completed)
            return completed
        finally:
            release_capacity_slot(repo, capacity_claim)
    return None


async def _run_sandbox_with_lease(
    repo: Repository,
    *,
    run: str,
    sandbox: Sandbox,
    command: list[str],
    cwd: str,
    env: dict[str, str],
    mask_env: dict[str, str],
    token: str,
    capacity_claim: dict[str, object] | None,
    logs: list[LogChunk],
    lease_ttl_s: int,
    heartbeat_interval_s: int,
    timeout_s: int,
) -> tuple[bool, ExecResult | None]:
    result: ExecResult | None = None
    iterator = sandbox.run(command, env, cwd, timeout_s).__aiter__()
    next_event = asyncio.create_task(anext(iterator))
    last_heartbeat = time.monotonic()
    try:
        while True:
            done, _pending = await asyncio.wait({next_event}, timeout=0.25)
            if not done:
                if _run_should_stop(repo, run, token):
                    await sandbox.cancel()
                    return True, result
                if heartbeat_interval_s > 0 and time.monotonic() - last_heartbeat >= heartbeat_interval_s:
                    if not heartbeat_run(repo, run, token=token, ttl_s=lease_ttl_s) or not heartbeat_capacity_slot(
                        repo,
                        capacity_claim,
                        ttl_s=lease_ttl_s,
                    ):
                        await sandbox.cancel()
                        return True, result
                    last_heartbeat = time.monotonic()
                continue
            try:
                event = next_event.result()
            except StopAsyncIteration:
                break
            next_event = asyncio.create_task(anext(iterator))
            if isinstance(event, LogChunk):
                masked_event = mask_log_chunk(event, mask_env)
                logs.append(masked_event)
                append_run_logs(repo, run, token=token, logs=[asdict(masked_event)])
            else:
                result = event
            if heartbeat_interval_s > 0 and time.monotonic() - last_heartbeat >= heartbeat_interval_s:
                if not heartbeat_run(repo, run, token=token, ttl_s=lease_ttl_s) or not heartbeat_capacity_slot(
                    repo,
                    capacity_claim,
                    ttl_s=lease_ttl_s,
                ):
                    await sandbox.cancel()
                    return True, result
                last_heartbeat = time.monotonic()
            if _run_should_stop(repo, run, token):
                await sandbox.cancel()
                return True, result
        return False, result
    finally:
        if not next_event.done():
            next_event.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await next_event


def _run_should_stop(repo: Repository, run: str, token: str) -> bool:
    try:
        payload = load_run(repo, run)
    except KeyError:
        return True
    state = payload.get("state")
    if not isinstance(state, dict):
        return True
    return state.get("phase") != "running" or state.get("token") != token


def _command_from_payload(payload: dict[str, object]) -> list[str]:
    command = payload.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("action run payload has no command")
    return command


def _spec_from_payload(payload: dict[str, object]) -> Spec:
    raw = payload.get("spec")
    if not isinstance(raw, dict):
        raise ValueError("action run payload has no spec")
    gpu_raw = raw.get("gpu")
    gpu = GPU(kind=str(gpu_raw["kind"]), count=int(gpu_raw["count"])) if isinstance(gpu_raw, dict) else None
    arch = raw.get("arch", _host_arch())
    network = raw.get("network", "default")
    return Spec(
        cpu=int(raw.get("cpu", 1)),
        memory_gib=int(raw.get("memory_gib", 1)),
        disk_gib=int(raw.get("disk_gib", 1)),
        arch=cast(Arch, arch),
        gpu=gpu,
        network=cast(NetworkMode, network),
    )


def _timeout_from_payload(payload: dict[str, object]) -> int:
    raw = payload.get("timeout_s", 30 * 60)
    timeout_s = int(raw) if isinstance(raw, int) else 30 * 60
    if timeout_s < 1:
        raise ValueError("timeout_s must be >= 1")
    return timeout_s


def _isolation_from_payload(payload: dict[str, object]) -> Isolation:
    raw = payload.get("isolation", "process")
    if raw not in {"process", "container", "microvm", "vm"}:
        raise ValueError("invalid isolation")
    return cast(Isolation, raw)


def _provider_from_payload(payload: dict[str, object]) -> str | None:
    raw = payload.get("provider_id")
    return raw if isinstance(raw, str) and raw else None


def _strict_provider_from_payload(payload: dict[str, object]) -> bool:
    return payload.get("strict_provider") is True


def _artifact_paths_from_payload(payload: dict[str, object]) -> tuple[str, ...]:
    raw = payload.get("artifact_paths")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return ()
    return tuple(raw)


async def _attach_artifacts_from_workspace(
    repo: Repository,
    payload: dict[str, object],
    *,
    sandbox: Sandbox,
    provider: SandboxProviderInfo,
    sandbox_cwd: str,
) -> dict[str, object]:
    artifact_paths = _artifact_paths_from_payload(payload)
    if not artifact_paths:
        return payload
    if "remote" not in provider.capabilities:
        return _attach_artifacts(repo, payload, root=sandbox_cwd)
    with tempfile.TemporaryDirectory(prefix="trunks-actions-artifacts-") as tmp:
        root = Path(tmp)
        downloads = []
        for raw_path in artifact_paths:
            source = raw_path if raw_path.startswith("/") else f"{sandbox_cwd.rstrip('/')}/{raw_path}"
            downloads.append(SandboxFile(source=source, target=str(root / raw_path.lstrip("/"))))
        with contextlib.suppress(Exception):
            async for _progress in sandbox.download(tuple(downloads)):
                pass
        return _attach_artifacts(repo, payload, root=str(root))


def _attach_artifacts(repo: Repository, payload: dict[str, object], *, root: str) -> dict[str, object]:
    artifact_paths = _artifact_paths_from_payload(payload)
    if not artifact_paths:
        return payload
    updated = dict(payload)
    updated["artifacts"] = collect_artifacts(repo, str(payload["id"]), root=root, paths=artifact_paths)
    return updated


def _requested_region(payload: dict[str, object]) -> str | None:
    state = payload.get("state")
    if isinstance(state, dict) and isinstance(state.get("requested_region"), str):
        return state["requested_region"]
    return None


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
