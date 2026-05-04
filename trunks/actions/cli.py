from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from trunks.ids import ulid
from trunks.objects import Blob
from trunks.repository import Repository
from trunks.sandboxes import Arch, GPU, Spec

from .artifacts import get_artifact, list_artifacts, write_artifact
from .backend_store import repo_root, store
from .capacity import capacity_snapshot, set_capacity_limit
from .health import provider_health_snapshot
from .oidc import mint_oidc_token
from .run import enqueue_command, run_command
from .secrets import bind_secret, list_secret_bindings, remove_secret_binding, show_secret_binding
from .storage import cancel_run, list_runs, load_run, prune_runs, repair_indexes
from .watch import watch_run
from .executor import execute_once
from .workflow import (
    WorkflowError,
    lint_workflows,
    list_workflow_runs,
    load_workflow_run,
    migrate_workflow,
    run_workflow,
    workflow_run_graph,
    workflow_run_jobs,
    workflow_run_logs,
)


async def dispatch(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="trunks actions")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run")
    run_p.add_argument("workflow_or_job", nargs="?")
    _add_command_args(run_p)
    run_p.add_argument("--provider", default=None)
    run_p.add_argument("--strict-provider", action="store_true")
    run_p.add_argument("--timeout", type=_positive_int, default=30 * 60)
    run_p.add_argument("--accept-best-effort", action="store_true")
    run_p.add_argument("--oidc-enabled", action="store_true")
    run_p.add_argument("--artifact", action="append", default=[])
    run_p.add_argument("--json", action="store_true")

    enqueue_p = sub.add_parser("enqueue")
    _add_command_args(enqueue_p)
    enqueue_p.add_argument("--provider", default=None)
    enqueue_p.add_argument("--strict-provider", action="store_true")
    enqueue_p.add_argument("--timeout", type=_positive_int, default=30 * 60)
    enqueue_p.add_argument("--artifact", action="append", default=[])
    enqueue_p.add_argument("--json", action="store_true")

    list_p = sub.add_parser("list", aliases=["ls"])
    list_p.add_argument("--status", default=None)
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("--offset", type=int, default=0)
    list_p.add_argument("--json", action="store_true")

    cancel_p = sub.add_parser("cancel")
    cancel_p.add_argument("--id", required=True)
    cancel_p.add_argument("--json", action="store_true")

    describe_p = sub.add_parser("describe", aliases=["get"])
    describe_p.add_argument("--id", required=True)
    describe_p.add_argument("--json", action="store_true")

    status_p = sub.add_parser("status")
    status_p.add_argument("--id", required=True)
    status_p.add_argument("--json", action="store_true")

    logs_p = sub.add_parser("logs")
    logs_p.add_argument("--id", required=True)
    logs_p.add_argument("--json", action="store_true")

    watch_p = sub.add_parser("watch")
    watch_p.add_argument("--id", required=True)
    watch_p.add_argument("--interval", type=float, default=0.25)
    watch_p.add_argument("--timeout", default=None)
    watch_p.add_argument("--json", action="store_true")

    artifacts_p = sub.add_parser("artifacts")
    artifacts_p.add_argument("--id", required=True)
    artifacts_p.add_argument("action", nargs="?", choices=["list", "get"], default="list")
    artifacts_p.add_argument("name", nargs="?")
    artifacts_p.add_argument("-o", "--output", default=None)
    artifacts_p.add_argument("--json", action="store_true")

    secrets_p = sub.add_parser("secrets")
    secrets_p.add_argument("action", choices=["list", "ls", "bind", "show", "remove", "rm"])
    secrets_p.add_argument("name", nargs="?")
    secrets_p.add_argument("source", nargs="?")
    secrets_p.add_argument("--json", action="store_true")

    capacity_p = sub.add_parser("capacity")
    capacity_p.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    capacity_p.add_argument("region", nargs="?")
    capacity_p.add_argument("spec_key", nargs="?")
    capacity_p.add_argument("field", nargs="?")
    capacity_p.add_argument("value", nargs="?")
    capacity_p.add_argument("--json", action="store_true")

    health_p = sub.add_parser("health")
    health_p.add_argument("--json", action="store_true")

    oidc_p = sub.add_parser("oidc")
    oidc_p.add_argument("action", choices=["token"])
    oidc_p.add_argument("--id", required=True)
    oidc_p.add_argument("--audience", required=True)
    oidc_p.add_argument("--issuer", default="https://trunks.local")
    oidc_p.add_argument("--ttl", type=_positive_int, default=600)
    oidc_p.add_argument("--json", action="store_true")

    index_p = sub.add_parser("index")
    index_p.add_argument("action", choices=["repair"])
    index_p.add_argument("--json", action="store_true")

    prune_p = sub.add_parser("prune")
    prune_p.add_argument("--older-than", default=None)
    prune_p.add_argument("--keep-last", type=int, default=None)
    prune_p.add_argument("--dry-run", action="store_true")
    prune_p.add_argument("--clean-objects", action="store_true")
    prune_p.add_argument("--json", action="store_true")

    storage_p = sub.add_parser("storage")
    storage_p.add_argument("action", choices=["doctor"])
    storage_p.add_argument("--json", action="store_true")

    execute_p = sub.add_parser("execute")
    execute_p.add_argument("--id", default="executor")
    execute_p.add_argument("--run-id", default=None)
    execute_p.add_argument("--provider", default=None)
    execute_p.add_argument("--region", default=None)
    execute_p.add_argument("--json", action="store_true")

    workflows_p = sub.add_parser("workflows")
    workflows_p.add_argument("action", nargs="?", choices=["list", "show", "lint"], default="list")
    workflows_p.add_argument("path", nargs="?")
    workflows_p.add_argument("--accept-best-effort", action="store_true")
    workflows_p.add_argument("--oidc-enabled", action="store_true")
    workflows_p.add_argument("--json", action="store_true")

    workflow_runs_p = sub.add_parser("workflow-runs")
    workflow_runs_p.add_argument("--id", default=None)
    workflow_runs_p.add_argument("view", nargs="?", choices=["jobs", "graph", "logs"])
    workflow_runs_p.add_argument("--status", default=None)
    workflow_runs_p.add_argument("--limit", type=int, default=50)
    workflow_runs_p.add_argument("--offset", type=int, default=0)
    workflow_runs_p.add_argument("--json", action="store_true")

    migrate_p = sub.add_parser("migrate")
    migrate_p.add_argument("path")
    migrate_p.add_argument("-o", "--output", default=None)
    migrate_p.add_argument("--dry-run", action="store_true")
    migrate_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "run":
        if not args.ad_hoc_command:
            try:
                payload = await run_workflow(
                    Repository.find(),
                    workflow=args.workflow_or_job,
                    commit=args.commit,
                    accept_best_effort=args.accept_best_effort,
                    oidc_enabled=args.oidc_enabled,
                )
            except WorkflowError as exc:
                parser.error(str(exc))
            if args.json:
                print(json.dumps(payload, sort_keys=True))
                return 0
            print(f"{payload['id']} {payload['phase']}")
            return 0
        run = await run_command(
            args.ad_hoc_command,
            commit=args.commit,
            timeout_s=args.timeout,
            provider_id=args.provider,
            strict_provider=args.strict_provider,
            region=args.region,
            spec=_spec_from_args(args),
            artifact_paths=tuple(args.artifact),
            isolation=args.isolation,
        )
        payload = run.to_dict()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        result = payload.get("result") or {}
        exit_code = result.get("exit_code") if isinstance(result, dict) else None
        print(f"{payload['id']} {payload['state']['phase']} exit={exit_code}")
        return 0
    if args.command == "enqueue":
        if not args.ad_hoc_command:
            parser.error("enqueue requires --command for the current implementation slice")
        run = enqueue_command(
            Repository.find(),
            args.ad_hoc_command,
            commit=args.commit,
            timeout_s=args.timeout,
            provider_id=args.provider,
            strict_provider=args.strict_provider,
            region=args.region,
            spec=_spec_from_args(args),
            artifact_paths=tuple(args.artifact),
            isolation=args.isolation,
        )
        payload = run.to_dict()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        _print_run_summary(payload)
        return 0
    if args.command in {"list", "ls"}:
        payload = {
            "_schema_version": "trunks.actions.run_list.v1",
            "object": "action_run_list",
            "data": list_runs(Repository.find(), status=args.status, limit=args.limit, offset=args.offset),
            "limit": args.limit,
            "offset": args.offset,
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        for run in payload["data"]:
            if isinstance(run, dict):
                _print_run_summary(run)
        return 0
    if args.command == "cancel":
        payload = cancel_run(Repository.find(), args.id)
        if payload is None:
            parser.error(f"unknown action run {args.id!r}")
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        _print_run_summary(payload)
        return 0
    if args.command in {"describe", "get"}:
        payload = load_run(Repository.find(), args.id)
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        _print_run_summary(payload)
        return 0
    if args.command == "status":
        payload = load_run(Repository.find(), args.id)
        state = payload.get("state") if isinstance(payload, dict) else None
        if args.json:
            print(json.dumps(state or {}, sort_keys=True))
            return 0
        if isinstance(state, dict):
            print(state.get("phase", "unknown"))
        return 0
    if args.command == "logs":
        payload = load_run(Repository.find(), args.id)
        logs = payload.get("logs") if isinstance(payload, dict) else None
        if args.json:
            print(
                json.dumps(
                    {
                        "_schema_version": "trunks.actions.logs.v1",
                        "object": "action_logs",
                        "run": args.id,
                        "data": logs or [],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if isinstance(logs, list):
            for item in logs:
                if isinstance(item, dict):
                    print(str(item.get("text", "")), end="")
        return 0
    if args.command == "watch":
        repo = Repository.find()
        timeout_s = None if args.timeout is None else _parse_duration(args.timeout)
        async for event in watch_run(repo, args.id, interval_s=args.interval, timeout_s=timeout_s):  # type: ignore[union-attr]
            if args.json:
                print(json.dumps(event, sort_keys=True), flush=True)
            elif event.get("object") == "action_log":
                print(str(event.get("text", "")), end="", flush=True)
            elif event.get("object") == "action_result":
                print(f"{event.get('phase')}", flush=True)
        return 0
    if args.command == "artifacts":
        repo = Repository.find()
        if args.action == "get":
            if not args.name:
                parser.error("artifacts get requires <name>")
            if args.output:
                write_artifact(repo, args.id, args.name, args.output)
                if args.json:
                    print(json.dumps({"object": "artifact_download", "run": args.id, "name": args.name, "output": args.output}, sort_keys=True))
                return 0
            sys.stdout.buffer.write(get_artifact(repo, args.id, args.name))
            return 0
        payload = {
            "_schema_version": "trunks.actions.artifact_list.v1",
            "object": "artifact_list",
            "run": args.id,
            "data": list_artifacts(repo, args.id),
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        for item in payload["data"]:
            if isinstance(item, dict):
                print(f"{item.get('name')} {item.get('size')} {item.get('oid')}")
        return 0
    if args.command == "secrets":
        repo = Repository.find()
        if args.action in {"list", "ls"}:
            payload = {
                "_schema_version": "trunks.actions.secret_binding_list.v1",
                "object": "secret_binding_list",
                "data": list_secret_bindings(repo),
            }
        elif args.action == "bind":
            if not args.name or not args.source:
                parser.error("secrets bind requires <name> <source>")
            payload = bind_secret(repo, args.name, args.source)
        elif args.action == "show":
            if not args.name:
                parser.error("secrets show requires <name>")
            payload = show_secret_binding(repo, args.name)
        else:
            if not args.name:
                parser.error("secrets remove requires <name>")
            payload = remove_secret_binding(repo, args.name)
        public_payload = _public_secret_payload(payload)
        if args.json:
            print(json.dumps(public_payload, sort_keys=True))
            return 0
        if args.action in {"list", "ls"} and isinstance(public_payload, dict) and isinstance(public_payload.get("data"), list):
            print(f"{len(public_payload['data'])} secret binding(s)")
        elif args.action == "bind":
            print("secret binding created")
        elif args.action == "show":
            print("secret binding details available via --json")
        else:
            print("secret binding removed")
        return 0
    if args.command == "capacity":
        repo = Repository.find()
        if args.action == "set":
            if not args.region or not args.spec_key or args.field != "max-concurrent" or args.value is None:
                parser.error("capacity set requires <region> <spec-key> max-concurrent <n>")
            payload = set_capacity_limit(
                repo,
                region=args.region,
                spec_key=args.spec_key,
                max_concurrent=int(args.value),
            )
        else:
            payload = capacity_snapshot(repo)
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.command == "health":
        payload = provider_health_snapshot(Repository.find())
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.command == "oidc":
        try:
            payload = mint_oidc_token(
                Repository.find(),
                run=args.id,
                audience=args.audience,
                issuer=args.issuer,
                ttl_s=args.ttl,
            )
        except RuntimeError as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print(payload["token"])
        return 0
    if args.command == "index":
        payload = repair_indexes(Repository.find())
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print(f"repaired {payload['repaired']} action run indexes")
        return 0
    if args.command == "prune":
        try:
            older_than_s = None if args.older_than is None else int(_parse_duration(args.older_than))
            payload = prune_runs(
                Repository.find(),
                older_than_s=older_than_s,
                keep_last=args.keep_last,
                dry_run=args.dry_run,
                clean_objects=args.clean_objects,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print(
            f"pruned runs={payload['deleted_run_refs']} workflow_runs={payload['deleted_workflow_run_refs']} "
            f"objects={payload['removed_objects']}"
        )
        return 0
    if args.command == "execute":
        result = await execute_once(
            Repository.find(),
            executor=args.id,
            provider_id=args.provider,
            region=args.region,
            run_id=args.run_id,
        )
        payload: dict[str, object] = {
            "_schema_version": "trunks.actions.execute.v1",
            "object": "action_execute",
            "run": result,
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        if result is not None:
            _print_run_summary(result)
        return 0
    if args.command == "storage":
        payload = _storage_doctor(Repository.find())
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print("ok" if payload["ok"] else "failed")
        return 0
    if args.command == "workflows":
        repo = Repository.find()
        root = repo.root
        if args.action == "lint" and args.path:
            candidate = Path(args.path)
            root = candidate.resolve().parent.parent.parent if candidate.name.endswith((".yml", ".yaml")) else candidate.resolve()
        data = lint_workflows(root, accept_best_effort=args.accept_best_effort, oidc_enabled=args.oidc_enabled)
        if args.action == "show":
            if not args.path:
                parser.error("workflows show requires <name>")
            matches = [
                item["workflow"]
                for item in data
                if item.get("valid") is True
                and isinstance(item.get("workflow"), dict)
                and (
                    item["workflow"].get("name") == args.path
                    or item["workflow"].get("path") == args.path
                    or Path(str(item["workflow"].get("path"))).stem == args.path
                )
            ]
            if not matches:
                parser.error(f"unknown workflow {args.path!r}")
            payload = matches[0]
        elif args.action == "list":
            data = [item["workflow"] for item in data if item.get("valid") is True]
            payload = {"_schema_version": "trunks.actions.workflow_list.v1", "object": "workflow_list", "data": data}
        else:
            payload = {"_schema_version": "trunks.actions.workflow_lint.v1", "object": "workflow_lint", "data": data}
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        for item in payload["data"]:
            if isinstance(item, dict):
                if item.get("valid") is False:
                    print(f"{item.get('path')} invalid: {item.get('error')}")
                else:
                    print(item.get("name") or item.get("path"))
        return 0
    if args.command == "workflow-runs":
        repo = Repository.find()
        if args.id and args.view == "jobs":
            payload = {
                "_schema_version": "trunks.actions.workflow_run_jobs.v1",
                "object": "workflow_run_jobs",
                "data": workflow_run_jobs(repo, args.id),
            }
        elif args.id and args.view == "graph":
            payload = {
                "_schema_version": "trunks.actions.workflow_run_graph.v1",
                "object": "workflow_run_graph",
                "data": workflow_run_graph(repo, args.id),
            }
        elif args.id and args.view == "logs":
            payload = {
                "_schema_version": "trunks.actions.workflow_run_logs.v1",
                "object": "workflow_run_logs",
                "data": workflow_run_logs(repo, args.id),
            }
        elif args.id:
            payload = load_workflow_run(repo, args.id)
        else:
            payload = {
                "_schema_version": "trunks.actions.workflow_run_list.v1",
                "object": "workflow_run_list",
                "data": list_workflow_runs(repo, status=args.status, limit=args.limit, offset=args.offset),
            }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.command == "migrate":
        payload = migrate_workflow(
            Path(args.path),
            output=None if args.output is None else Path(args.output),
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(payload, sort_keys=True))
            return 0
        print(f"{payload['source']} -> {payload['target']}")
        return 0
    parser.print_help()
    return 0


def _public_secret_payload(payload: dict[str, object]) -> dict[str, object]:
    if isinstance(payload.get("data"), list):
        return {**payload, "data": [_public_secret_binding(item) for item in payload["data"] if isinstance(item, dict)]}
    return _public_secret_binding(payload)


def _public_secret_binding(binding: dict[str, object]) -> dict[str, object]:
    public: dict[str, object] = {}
    if "name" in binding:
        public["name"] = binding.get("name")
    source = binding.get("source")
    if isinstance(source, str) and ":" in source:
        public["source_type"] = source.split(":", 1)[0]
    elif isinstance(source, str):
        public["source_type"] = "unknown"
    return public


def _add_command_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--command", dest="ad_hoc_command", default=None)
    parser.add_argument("--commit", default="worktree")
    parser.add_argument("--region", default=None)
    parser.add_argument("--cpu", type=_positive_int, default=None)
    parser.add_argument("--memory", type=_resource_gib, default=None)
    parser.add_argument("--disk", type=_resource_gib, default=None)
    parser.add_argument("--arch", choices=["x86_64", "arm64"], default=None)
    parser.add_argument("--isolation", choices=["process", "container", "microvm", "vm"], default="process")
    parser.add_argument("--gpu", default=None)
    parser.add_argument("--gpu-count", type=_positive_int, default=1)
    parser.add_argument("--network", choices=["default", "none", "egress-only"], default=None)


def _print_run_summary(run: dict[str, object]) -> None:
    state = run.get("state") if isinstance(run, dict) else None
    result = run.get("result") if isinstance(run, dict) else None
    phase = state.get("phase") if isinstance(state, dict) else "unknown"
    provider = state.get("provider") if isinstance(state, dict) else None
    region = state.get("region") if isinstance(state, dict) else None
    exit_code = result.get("exit_code") if isinstance(result, dict) else None
    print(f"{run.get('id')} {phase} provider={provider} region={region} exit={exit_code}")


def _spec_from_args(args: argparse.Namespace) -> Spec | None:
    if not any(item is not None for item in (args.cpu, args.memory, args.disk, args.arch, args.gpu, args.network)):
        return None
    gpu = GPU(kind=args.gpu, count=args.gpu_count) if args.gpu else None
    return Spec(
        cpu=args.cpu or 1,
        memory_gib=args.memory or 1,
        disk_gib=args.disk or 1,
        arch=args.arch or _host_arch(),
        gpu=gpu,
        network=args.network or "default",
    )


def _storage_doctor(repo: Repository) -> dict[str, object]:
    action_store = store(repo)
    token = ulid()
    blob = Blob.from_data(f"trunks actions storage doctor {token}\n".encode())
    ref = f"{repo_root(repo)}/doctor/{token}"
    action_store.write_blob(blob)
    action_store.set_ref(ref, blob.id)
    ok = action_store.read_ref(ref) == blob.id and action_store.read_blob_payload(blob.id).startswith(b"trunks actions")
    action_store.delete_ref(ref)
    return {
        "_schema_version": "trunks.actions.storage_doctor.v1",
        "object": "actions_storage_doctor",
        "repo": repo.name,
        "shared": action_store.shared,
        "ok": ok,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _resource_gib(value: str) -> int:
    raw = value.strip().lower()
    multiplier = 1.0
    number = raw
    for suffix, factor in (("gib", 1.0), ("gb", 1.0), ("mib", 1 / 1024), ("mb", 1 / 1024)):
        if raw.endswith(suffix):
            number = raw[: -len(suffix)]
            multiplier = factor
            break
    try:
        parsed = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a size like 8gb or 512mb") from exc
    gib = int(parsed * multiplier)
    if gib < 1:
        raise argparse.ArgumentTypeError("must be >= 1gb")
    return gib


def _parse_duration(value: str) -> float:
    raw = value.strip().lower()
    if not raw:
        raise ValueError("duration is required")
    unit = raw[-1]
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    if multiplier is None:
        return float(raw)
    return float(raw[:-1]) * multiplier


def _host_arch() -> Arch:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


def main() -> None:
    raise SystemExit(asyncio.run(dispatch(sys.argv[1:])))
