from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from getpass import getpass
from pathlib import Path

from .config import (
    GlobalConfig,
    backend_from_env,
    env_forces_local_only,
    get_storage_profile as get_global_storage_profile,
    mirror_backends_from_env,
    mirror_policy_from_env,
    push_mode_from_env,
    repo_name_from_env,
    resolve_backend_url,
    remove_storage_profile as remove_global_storage_profile,
    set_storage_profile as set_global_storage_profile,
)
from . import audit
from .cache import CacheManager
from .errors import RepositoryNotFound, TrunksError
from .gitcache import GIT_NOT_FOUND_MESSAGE, GitCache, find_system_git
from .gitimport import import_existing_git_repository
from .journal import JournalEntry
from .mount import registry as mount_registry
from .objects import Blob
from .repository import Repository
from . import runtime as repo_runtime
from . import webhooks
from .storage import Storage
from .trunk import Trunk
from .version import __version__
from .ids import ObjectId, ulid
from .paths import is_ignored, is_internal_path
from .url import BackendURL, backend_from_storage, backend_from_url


def main() -> None:
    raise SystemExit(asyncio.run(dispatch(sys.argv[1:])))


async def dispatch(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="trunks")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--backend", default=None)
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init")
    init_p.add_argument("--backend", default=None)
    init_p.add_argument("--name", default=None)

    mount_p = sub.add_parser("mount", help="create or open a Trunks repo at a path")
    mount_p.add_argument("--repo", required=True, help="logical repo name, for example my-app or acme/my-app")
    mount_p.add_argument("--path", default=None, help="local mount path")
    mount_p.add_argument("--backend", default=None, help="optional backend URL/base for this repo")
    mount_p.add_argument("--require-existing", action="store_true", help="fail if --path is not already a Trunks repo")
    mount_p.add_argument(
        "--mode",
        choices=["virtual", "disk"],
        default="disk",
        help="disk uses a normal directory; virtual uses localhost NFSv3 for sparse filesystem virtualization",
    )
    mount_p.add_argument("--watch", action="store_true", help="start a background watcher that journals file changes")

    repo_p = sub.add_parser("repo", help="manage Trunks repositories")
    repo_p.add_argument("action", nargs="?", choices=["list", "create", "get", "update", "delete"], default="list")
    repo_p.add_argument("--name", default=None)
    repo_p.add_argument("--path", default=None)
    repo_p.add_argument("--backend", default=None)
    repo_p.add_argument("--json", action="store_true")
    repo_p.add_argument("--limit", type=int, default=100)
    repo_p.add_argument("--offset", type=int, default=0)

    unmount_p = sub.add_parser("unmount", help="stop a mounted repo daemon when one is running")
    unmount_p.add_argument("--path", default=None)

    clone_p = sub.add_parser("clone", help="initialize a repo from a backend URL and pull")
    clone_p.add_argument("backend")
    clone_p.add_argument("dest", nargs="?", default=".")
    clone_p.add_argument("--name", default=None)

    config_p = sub.add_parser("config")
    config_p.add_argument("action", choices=["set", "get"])
    config_p.add_argument("key")
    config_p.add_argument("value", nargs="?")

    status_p = sub.add_parser("status")
    status_p.add_argument("--json", action="store_true")
    status_p.add_argument("--path", default=None)
    doctor_p = sub.add_parser("doctor", help="diagnose the current Trunks repo")
    doctor_p.add_argument("--json", action="store_true")
    doctor_p.add_argument("--path", default=None)
    doctor_p.add_argument("--ping", action="store_true", help="also validate configured storage with network/backend IO")
    sub.add_parser("push")
    sub.add_parser("pull")
    sub.add_parser("fetch")
    check_p = sub.add_parser("check")
    check_p.add_argument("--json", action="store_true")
    check_p.add_argument("--clean", action="store_true")
    storage_p = sub.add_parser("storage")
    storage_p.add_argument("action", nargs="?", choices=["add", "create", "update", "remove", "delete", "ping", "list", "show", "get", "wizard"])
    storage_p.add_argument("values", nargs="*")
    storage_p.add_argument("--name", dest="storage_name", default=None)
    storage_p.add_argument("--url", dest="storage_url", default=None)
    storage_p.add_argument("--backend", dest="storage_backend", default=None)
    storage_p.add_argument("--mirror", action="store_true")
    storage_p.add_argument("--bucket", default=None)
    storage_p.add_argument("--region", default=None)
    storage_p.add_argument("--endpoint", default=None)
    storage_p.add_argument("--prefix", default=None)
    storage_p.add_argument("--account-id", default=None)
    storage_p.add_argument("--access-key", default=None)
    storage_p.add_argument("--secret-key", default=None)
    storage_p.add_argument("--session-token", default=None)
    storage_p.add_argument("--path", default=None)
    storage_p.add_argument("--host", default=None)
    storage_p.add_argument("--port", default=None)
    storage_p.add_argument("--user", default=None)
    storage_p.add_argument("--password", default=None)
    storage_p.add_argument("--password-env", default=None)
    storage_p.add_argument("--ssh-key", default=None)
    storage_p.add_argument("--key-passphrase", default=None)
    storage_p.add_argument("--dsn", default=None)
    storage_p.add_argument("--dsn-env", default=None)
    storage_p.add_argument("--container", default=None)
    storage_p.add_argument("--account-name", default=None)
    storage_p.add_argument("--account-key", default=None)
    storage_p.add_argument("--sas-token", default=None)
    storage_p.add_argument("--service-account-file", default=None)
    storage_p.add_argument("--project", default=None)
    storage_p.add_argument("--json", action="store_true")
    storage_p.add_argument("--limit", type=int, default=100)
    storage_p.add_argument("--offset", type=int, default=0)
    cache_p = sub.add_parser("cache", help="inspect or maintain the local object cache")
    cache_p.add_argument("action", choices=["stats", "clear", "prune", "verify"])
    cache_p.add_argument("--json", action="store_true")
    cache_p.add_argument("--max-size", default="5GB")
    webhook_p = sub.add_parser("webhook", help="manage signed push webhooks")
    webhook_p.add_argument("action", choices=["add", "create", "get", "update", "list", "delete"])
    webhook_p.add_argument("value", nargs="?")
    webhook_p.add_argument("--url", default=None)
    webhook_p.add_argument("--on", default=None)
    webhook_p.add_argument("--json", action="store_true")
    webhook_p.add_argument("--limit", type=int, default=100)
    webhook_p.add_argument("--offset", type=int, default=0)
    audit_p = sub.add_parser("audit", help="inspect the local audit event log")
    audit_p.add_argument("action", nargs="?", choices=["list"], default="list")
    audit_p.add_argument("--json", action="store_true")
    audit_p.add_argument("--limit", type=int, default=None, help="show at most N most recent events")
    audit_p.add_argument("--offset", type=int, default=0, help="skip N events in JSON output")
    log_p = sub.add_parser("log")
    log_p.add_argument("--json", action="store_true")
    history_p = sub.add_parser("history", help="show refs and commit graph")
    history_p.add_argument("--json", action="store_true")

    actions_p = sub.add_parser("actions", help="run Trunks Actions workflows and jobs")
    actions_p.add_argument("module_args", nargs=argparse.REMAINDER)

    sandboxes_p = sub.add_parser("sandboxes", help="inspect sandbox providers")
    sandboxes_p.add_argument("module_args", nargs=argparse.REMAINDER)

    diff_p = sub.add_parser("diff", help="show file changes between refs or the worktree")
    diff_p.add_argument("--from", dest="from_ref", default=None)
    diff_p.add_argument("--vs", default=None)
    diff_p.add_argument("--json", action="store_true")

    add_p = sub.add_parser("add", help="stage paths into trunks (also stages in .git)")
    add_p.add_argument("paths", nargs="*")
    add_p.add_argument("-f", "--force", action="store_true")

    commit_p = sub.add_parser("commit", help="record a commit in trunks")
    commit_p.add_argument("-m", "--message", default="")
    commit_p.add_argument("-a", "--all", action="store_true", help="stage tracked changes first")

    checkpoint_p = sub.add_parser("checkpoint", help="stage all worktree files and create a commit if changed")
    checkpoint_p.add_argument("-m", "--message", default="")

    branch_p = sub.add_parser("branch", help="manage branches")
    branch_p.add_argument("action", choices=["list", "create", "get", "update", "switch", "delete"])
    branch_p.add_argument("--name", default=None)
    branch_p.add_argument("--from", dest="from_ref", default=None)
    branch_p.add_argument("--json", action="store_true")
    branch_p.add_argument("--limit", type=int, default=100)
    branch_p.add_argument("--offset", type=int, default=0)

    tag_p = sub.add_parser("tag", help="manage tags")
    tag_p.add_argument("action", nargs="?")
    tag_p.add_argument("value", nargs="?")
    tag_p.add_argument("--name", default=None)
    tag_p.add_argument("--at", default=None)
    tag_p.add_argument("--json", action="store_true")
    tag_p.add_argument("--limit", type=int, default=100)
    tag_p.add_argument("--offset", type=int, default=0)

    rollback_p = sub.add_parser("rollback", help="move the current branch to a ref or commit")
    rollback_p.add_argument("--to", required=True)

    checkout_p = sub.add_parser("checkout", help="switch branch")
    checkout_p.add_argument("name")
    checkout_p.add_argument("-b", "--create", action="store_true")

    shim_p = sub.add_parser("shim")
    shim_p.add_argument("action", choices=["install", "print", "path"])
    shim_p.add_argument("dest", nargs="?")

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return init(args.name, args.backend)
        if args.command == "mount":
            return await mount(
                args.repo,
                args.path,
                backend=args.backend,
                require_existing=args.require_existing,
                mode=args.mode,
                watch=args.watch,
            )
        if args.command == "repo":
            return repo_cmd(
                args.action,
                name=args.name,
                path=args.path,
                backend=args.backend,
                json_output=args.json,
                limit=args.limit,
                offset=args.offset,
            )
        if args.command == "unmount":
            return await unmount(args.path)
        if args.command == "clone":
            return await clone(args.backend, args.dest, args.name)
        if args.command == "config":
            return config(args.action, args.key, args.value)
        if args.command == "status":
            return await status(json_output=args.json, path=args.path)
        if args.command == "doctor":
            return await doctor(json_output=args.json, path=args.path, ping=args.ping)
        if args.command == "push":
            return await run_trunk("push")
        if args.command == "pull":
            return await run_trunk("pull")
        if args.command == "fetch":
            return await run_trunk("fetch")
        if args.command == "check":
            return check(json_output=args.json, clean=args.clean)
        if args.command == "storage":
            return await storage_from_args(args)
        if args.command == "cache":
            return cache(args.action, json_output=args.json, max_size=args.max_size)
        if args.command == "webhook":
            return webhook(
                args.action,
                args.value,
                url=args.url,
                events=args.on,
                json_output=args.json,
                limit=args.limit,
                offset=args.offset,
            )
        if args.command == "audit":
            return audit_cmd(args.action, json_output=args.json, limit=args.limit, offset=args.offset)
        if args.command == "log":
            return await log(json_output=args.json)
        if args.command == "history":
            return history(json_output=args.json)
        if args.command == "actions":
            from .actions.cli import dispatch as actions_dispatch

            return await actions_dispatch(args.module_args)
        if args.command == "sandboxes":
            from .sandboxes.cli import dispatch as sandboxes_dispatch

            return await sandboxes_dispatch(args.module_args)
        if args.command == "diff":
            return cmd_diff(from_ref=args.from_ref, vs=args.vs, json_output=args.json)
        if args.command == "add":
            return cmd_add(args.paths, args.force)
        if args.command == "commit":
            return cmd_commit(args.message, args.all)
        if args.command == "checkpoint":
            return cmd_checkpoint(args.message)
        if args.command == "branch":
            return cmd_branch(
                args.action,
                name=args.name,
                from_ref=args.from_ref,
                json_output=args.json,
                limit=args.limit,
                offset=args.offset,
            )
        if args.command == "tag":
            return cmd_tag(
                args.action,
                args.value,
                name=args.name,
                at=args.at,
                json_output=args.json,
                limit=args.limit,
                offset=args.offset,
            )
        if args.command == "rollback":
            return cmd_rollback(args.to)
        if args.command == "checkout":
            return cmd_checkout(args.name, args.create)
        if args.command == "shim":
            return shim(args.action, args.dest)
        return shell(args.backend)
    except TrunksError as exc:
        print(f"trunks: {exc}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        print(f"trunks: corrupt repository database ({exc})", file=sys.stderr)
        return 1


def cmd_add(paths: list[str], force: bool) -> int:
    repo = _repo_or_init()
    targets = paths or ["."]
    for target in targets:
        path = Path(target).resolve() if Path(target).is_absolute() else (Path.cwd() / target).resolve()
        repo.add_worktree_path(path, force=force)
    return 0


def cmd_commit(message: str, stage_all: bool) -> int:
    repo = _repo_or_init()
    if stage_all:
        repo.add_worktree_path(repo.root, force=False)
    if not message:
        print("trunks: commit message is required (-m)", file=sys.stderr)
        return 1
    commit = repo.create_commit(message=message)
    GitCache(repo).rebuild()
    print(f"[{repo.current_branch} {commit.id.short(7)}] {message}")
    return 0


def cmd_checkpoint(message: str) -> int:
    repo, virtual_record = _resolve_repo()
    if virtual_record is None:
        repo.add_all_worktree_files(force=False)
    if not message:
        message = "checkpoint"
    current = repo.ref(repo.current_branch)
    next_tree = repo.build_tree(repo.index_entries())
    if current is not None and repo.load_commit(current).tree == next_tree.id:
        print("No changes to checkpoint")
        return 0
    commit = repo.create_commit(message=message)
    GitCache(repo).rebuild()
    audit.record(
        repo,
        "checkpoint",
        {"commit": commit.id.value, "branch": repo.current_branch, "message": message},
    )
    print(f"[{repo.current_branch} {commit.id.short(7)}] {message}")
    return 0


def _api_list(items: list[dict[str, object]], *, limit: int | None, offset: int = 0) -> dict[str, object]:
    safe_offset = max(offset, 0)
    safe_limit = len(items) if limit is None else max(limit, 0)
    data = items[safe_offset:safe_offset + safe_limit]
    return {
        "object": "list",
        "data": data,
        "limit": safe_limit,
        "offset": safe_offset,
        "total_count": len(items),
        "has_more": safe_offset + len(data) < len(items),
    }


def _print_api_list(items: list[dict[str, object]], *, limit: int | None, offset: int) -> None:
    print(json.dumps(_api_list(items, limit=limit, offset=offset), sort_keys=True))


def _branch_record(repo: Repository, ref_name: str, oid: ObjectId) -> dict[str, object]:
    name = ref_name.removeprefix("refs/heads/")
    return {
        "object": "branch",
        "id": name,
        "name": name,
        "ref": ref_name,
        "head": oid.value,
        "current": name == repo.current_branch,
    }


def _branch_records(repo: Repository) -> list[dict[str, object]]:
    return [
        _branch_record(repo, ref_name, oid)
        for ref_name, oid in repo.list_refs()
        if ref_name.startswith("refs/heads/")
    ]


def _tag_record(ref_name: str, oid: ObjectId) -> dict[str, object]:
    name = ref_name.removeprefix("refs/tags/")
    return {
        "object": "tag",
        "id": name,
        "name": name,
        "ref": ref_name,
        "target": oid.value,
    }


def _tag_records(repo: Repository) -> list[dict[str, object]]:
    return [
        _tag_record(ref_name, oid)
        for ref_name, oid in repo.list_refs()
        if ref_name.startswith("refs/tags/")
    ]


def _repo_record(repo: Repository) -> dict[str, object]:
    head = repo.ref(repo.current_branch)
    return {
        "object": "repo",
        "id": repo.name,
        "name": repo.name,
        "path": str(repo.root),
        "repo_data_path": str(repo.path),
        "current_branch": repo.current_branch,
        "head": head.value if head else None,
        "backend": repo.backend_url(),
        "storage": repo.storage_targets(),
    }


def _storage_record(item: dict[str, object]) -> dict[str, object]:
    name = str(item.get("name", ""))
    return {
        "object": "storage_target",
        "id": name,
        **item,
    }


def _webhook_record(item: dict[str, object]) -> dict[str, object]:
    hook_id = str(item.get("id", ""))
    return {
        "object": "webhook",
        **item,
        "id": hook_id,
    }


def _audit_record(item: dict[str, object]) -> dict[str, object]:
    event_id = str(item.get("id", ""))
    return {
        "object": "audit_event",
        **item,
        "id": event_id,
    }


def _discover_repositories() -> list[Repository]:
    repos: dict[str, Repository] = {}
    try:
        current = Repository.find()
    except RepositoryNotFound:
        current = None
    if current is not None:
        repos[str(current.path)] = current
    home = Path(os.environ.get("TRUNKS_HOME", str(Path.home() / ".trunks")))
    for path in sorted((home / "repos").glob("*/.trunks/*.trunk")):
        try:
            repo = Repository(path)
        except TrunksError:
            continue
        repos.setdefault(str(repo.path), repo)
    return sorted(repos.values(), key=lambda repo: repo.name)


def _managed_repo_root(name: str) -> Path:
    home = Path(os.environ.get("TRUNKS_HOME", str(Path.home() / ".trunks")))
    return home / "repos" / name


def repo_cmd(
    action: str,
    *,
    name: str | None,
    path: str | None,
    backend: str | None,
    json_output: bool,
    limit: int,
    offset: int,
) -> int:
    if action == "list":
        records = [_repo_record(repo) for repo in _discover_repositories()]
        if json_output:
            _print_api_list(records, limit=limit, offset=offset)
            return 0
        for record in records:
            print(record["name"])
        return 0

    if action == "create":
        if not name:
            print("usage: trunks repo create --name <name> [--path <path>] [--backend <url>]", file=sys.stderr)
            return 1
        target = (Path(path).expanduser() if path else _managed_repo_root(name)).resolve()
        target.mkdir(parents=True, exist_ok=True)
        resolved_backend = resolve_backend_url(backend, name) if backend else _default_backend_for_repo(name)
        repo = Repository.init(target, name=name, backend=resolved_backend)
        if json_output:
            print(json.dumps(_repo_record(repo), sort_keys=True))
            return 0
        print(f"Created repo {repo.name}")
        return 0

    repo = _find_repo_resource(name=name, path=path)
    if repo is None:
        label = name or path or "current directory"
        print(f"trunks repo: not found: {label}", file=sys.stderr)
        return 1

    if action == "get":
        if json_output:
            print(json.dumps(_repo_record(repo), sort_keys=True))
            return 0
        print(repo.name)
        return 0

    if action == "update":
        if not backend:
            print("usage: trunks repo update [--name <name>] [--path <path>] --backend <url>", file=sys.stderr)
            return 1
        resolved = resolve_backend_url(backend, repo.name)
        repo.set_backend_url(resolved or "")
        if json_output:
            print(json.dumps(_repo_record(repo), sort_keys=True))
            return 0
        print(f"Updated repo {repo.name}")
        return 0

    if action == "delete":
        deleted = _delete_repo_resource(repo)
        if json_output:
            print(json.dumps(deleted, sort_keys=True))
            return 0
        print(f"Deleted repo {deleted['name']}")
        return 0

    print("usage: trunks repo [list|create|get|update|delete]", file=sys.stderr)
    return 1


def _default_backend_for_repo(repo_name: str) -> str | None:
    env_backend = backend_from_env(repo_name)
    if env_backend is not None:
        return env_backend
    if env_forces_local_only():
        return None
    return resolve_backend_url(GlobalConfig.load().default_backend, repo_name)


def _find_repo_resource(*, name: str | None, path: str | None) -> Repository | None:
    if path is not None:
        try:
            return Repository.find(Path(path).expanduser().resolve())
        except RepositoryNotFound:
            return None
    if name is None:
        try:
            return Repository.find()
        except RepositoryNotFound:
            return None
    return next((repo for repo in _discover_repositories() if repo.name == name), None)


def _delete_repo_resource(repo: Repository) -> dict[str, object]:
    name = repo.name
    path = repo.path
    if path.exists():
        path.unlink()
    try:
        if path.parent.name == ".trunks" and not any(path.parent.iterdir()):
            path.parent.rmdir()
    except OSError:
        pass
    return {"object": "repo", "id": name, "name": name, "deleted": True}


def _parse_webhook_events(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def cmd_branch(
    action: str,
    *,
    name: str | None,
    from_ref: str | None,
    json_output: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> int:
    repo = _repo_or_init()
    if action == "create":
        if not name:
            print("usage: trunks branch create --name <name> [--from <ref>]", file=sys.stderr)
            return 1
        repo.create_branch(name, start=from_ref, switch=False)
        GitCache(repo).rebuild()
        audit.record(repo, "branch.create", {"name": name, "from": from_ref})
        if json_output:
            oid = repo.ref(name)
            if oid is not None:
                print(json.dumps(_branch_record(repo, f"refs/heads/{name}", oid), sort_keys=True))
            return 0
        print(f"Created branch {name}")
        return 0
    if action == "get":
        if not name:
            print("usage: trunks branch get --name <name> [--json]", file=sys.stderr)
            return 1
        oid = repo.ref(name)
        if oid is None:
            print(f"trunks branch: unknown branch {name}", file=sys.stderr)
            return 1
        record = _branch_record(repo, f"refs/heads/{name}", oid)
        if json_output:
            print(json.dumps(record, sort_keys=True))
            return 0
        marker = "*" if record["current"] else " "
        print(f"{marker} {record['name']} {record['head']}")
        return 0
    if action == "update":
        if not name or not from_ref:
            print("usage: trunks branch update --name <name> --from <ref>", file=sys.stderr)
            return 1
        target = _resolve_commitish(repo, from_ref)
        if target is None:
            print(f"trunks branch: unknown ref or commit {from_ref!r}", file=sys.stderr)
            return 1
        repo.set_ref(f"refs/heads/{name}", target)
        GitCache(repo).rebuild()
        audit.record(repo, "branch.update", {"name": name, "from": from_ref})
        record = _branch_record(repo, f"refs/heads/{name}", target)
        if json_output:
            print(json.dumps(record, sort_keys=True))
            return 0
        print(f"Updated branch {name}")
        return 0
    if action == "switch":
        if not name:
            print("usage: trunks branch switch --name <name>", file=sys.stderr)
            return 1
        repo.checkout(name)
        GitCache(repo).rebuild()
        audit.record(repo, "branch.switch", {"name": name})
        if json_output:
            oid = repo.ref(name)
            if oid is not None:
                print(json.dumps(_branch_record(repo, f"refs/heads/{name}", oid), sort_keys=True))
            return 0
        print(f"Switched to branch '{name}'")
        return 0
    if action == "delete":
        if not name:
            print("usage: trunks branch delete --name <name>", file=sys.stderr)
            return 1
        if name == repo.current_branch:
            print(f"trunks: cannot delete current branch '{name}'", file=sys.stderr)
            return 1
        repo.delete_ref(name)
        GitCache(repo).rebuild()
        audit.record(repo, "branch.delete", {"name": name})
        if json_output:
            print(json.dumps({"object": "branch", "id": name, "name": name, "deleted": True}, sort_keys=True))
            return 0
        print(f"Deleted branch {name}")
        return 0
    if action == "list":
        records = _branch_records(repo)
        if json_output:
            _print_api_list(records, limit=limit, offset=offset)
            return 0
        for record in records:
            marker = "*" if record["current"] else " "
            print(f"{marker} {record['name']}")
        return 0
    print("usage: trunks branch [list|create|get|update|switch|delete] [--name <name>]", file=sys.stderr)
    return 1


def cmd_tag(
    action: str | None,
    value: str | None,
    *,
    name: str | None,
    at: str | None,
    json_output: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> int:
    repo = _repo_or_init()
    if action in {None, "list"}:
        records = _tag_records(repo)
        if json_output:
            _print_api_list(records, limit=limit, offset=offset)
            return 0
        for record in records:
            print(record["name"])
        return 0
    tag_name = name or value
    if action == "get":
        if not tag_name:
            print("usage: trunks tag get --name <name>", file=sys.stderr)
            return 1
        target = repo.ref(f"refs/tags/{tag_name}")
        if target is None:
            print(f"trunks tag: unknown tag {tag_name}", file=sys.stderr)
            return 1
        record = _tag_record(f"refs/tags/{tag_name}", target)
        if json_output:
            print(json.dumps(record, sort_keys=True))
            return 0
        print(f"{record['name']} {record['target']}")
        return 0
    if action == "create":
        if not tag_name:
            print("usage: trunks tag create --name <name> [--at <ref>]", file=sys.stderr)
            return 1
        target = repo.ref(at or repo.current_branch)
        if target is None:
            print(f"trunks: unknown ref: {at or repo.current_branch}", file=sys.stderr)
            return 1
        repo.set_ref(f"refs/tags/{tag_name}", target)
        GitCache(repo).rebuild()
        audit.record(repo, "tag.create", {"name": tag_name, "at": at or repo.current_branch})
        if json_output:
            print(json.dumps(_tag_record(f"refs/tags/{tag_name}", target), sort_keys=True))
            return 0
        print(f"Created tag {tag_name}")
        return 0
    if action == "update":
        if not tag_name or not at:
            print("usage: trunks tag update --name <name> --at <ref>", file=sys.stderr)
            return 1
        target = _resolve_commitish(repo, at)
        if target is None:
            print(f"trunks tag: unknown ref or commit {at!r}", file=sys.stderr)
            return 1
        repo.set_ref(f"refs/tags/{tag_name}", target)
        GitCache(repo).rebuild()
        audit.record(repo, "tag.update", {"name": tag_name, "at": at})
        record = _tag_record(f"refs/tags/{tag_name}", target)
        if json_output:
            print(json.dumps(record, sort_keys=True))
            return 0
        print(f"Updated tag {tag_name}")
        return 0
    if action == "delete":
        if not tag_name:
            print("usage: trunks tag delete --name <name>", file=sys.stderr)
            return 1
        repo.delete_ref(f"refs/tags/{tag_name}")
        GitCache(repo).rebuild()
        audit.record(repo, "tag.delete", {"name": tag_name})
        if json_output:
            print(json.dumps({"object": "tag", "id": tag_name, "name": tag_name, "deleted": True}, sort_keys=True))
            return 0
        print(f"Deleted tag {tag_name}")
        return 0
    print("usage: trunks tag [list|create|get|update|delete] [--name <name>]", file=sys.stderr)
    return 1


def cmd_diff(*, from_ref: str | None, vs: str | None, json_output: bool) -> int:
    repo = _repo_or_init()
    base_name = vs or repo.current_branch
    base_oid = _resolve_commitish(repo, base_name)
    if base_oid is None and vs is not None:
        # User explicitly named a ref/commit that does not exist — surface it.
        print(f"trunks diff: unknown ref or commit {base_name!r}", file=sys.stderr)
        return 1
    if base_oid is None:
        # Implicit base (current_branch with no commits yet) — treat as empty tree
        # so a brand-new repo can still `trunks diff` and see worktree adds.
        base: dict[str, ObjectId] = {}
    else:
        base = {
            entry.path: entry.oid
            for entry in repo.flatten_tree(repo.load_commit(base_oid).tree)
        }
    try:
        target = _entries_for_ref(repo, from_ref) if from_ref else _worktree_entries(repo)
    except TrunksError as exc:
        print(f"trunks diff: {exc}", file=sys.stderr)
        return 1
    changes = _diff_entries(base, target)
    if json_output:
        print(json.dumps(changes, sort_keys=True))
        return 0
    for change in changes:
        print(f"{change['status']} {change['path']}")
    return 0


def cmd_rollback(target: str) -> int:
    repo = _repo_or_init()
    oid = _resolve_commitish(repo, target)
    if oid is None:
        print(f"trunks rollback: unknown ref or commit {target!r}", file=sys.stderr)
        return 1
    previous_entries = repo.index_entries()
    repo.set_ref(repo.current_branch, oid)
    repo.reset_index_to_commit(oid)
    repo.checkout_tree(oid, previous_entries=previous_entries)
    GitCache(repo).rebuild()
    audit.record(repo, "rollback", {"to": target, "branch": repo.current_branch, "head": oid.value})
    print(f"Rolled back {repo.current_branch} to {target}")
    return 0


def _resolve_commitish(repo: Repository, value: str) -> ObjectId | None:
    oid = repo.ref(value)
    if oid is not None:
        return oid
    try:
        candidate = ObjectId(value)
    except ValueError:
        return None
    try:
        repo.load_commit(candidate)
    except Exception:
        return None
    return candidate


def _entries_for_ref(repo: Repository, ref: str | None) -> dict[str, ObjectId]:
    if ref is None:
        return {entry.path: entry.oid for entry in repo.index_entries()}
    oid = _resolve_commitish(repo, ref)
    if oid is None:
        raise TrunksError(f"unknown ref or commit {ref!r}")
    return {entry.path: entry.oid for entry in repo.flatten_tree(repo.load_commit(oid).tree)}


def _worktree_entries(repo: Repository) -> dict[str, ObjectId]:
    entries: dict[str, ObjectId] = {}
    for path in sorted(repo.root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = repo._relative_worktree_path(path.resolve(strict=False))
        if is_internal_path(rel) or is_ignored(rel, repo.root):
            continue
        normalized = repo._normalize_trackable_path(rel)
        entries[normalized] = Blob.from_data(path.read_bytes()).id
    return entries


def _diff_entries(base: dict[str, ObjectId], target: dict[str, ObjectId]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for path in sorted(set(base) | set(target)):
        if path not in base:
            changes.append({"status": "A", "path": path})
        elif path not in target:
            changes.append({"status": "D", "path": path})
        elif base[path] != target[path]:
            changes.append({"status": "M", "path": path})
    return changes


def cmd_checkout(name: str, create: bool) -> int:
    repo = _repo_or_init()
    if create:
        repo.create_branch(name)
        GitCache(repo).rebuild()
        print(f"Switched to a new branch '{name}'")
        return 0
    repo.checkout(name)
    GitCache(repo).rebuild()
    print(f"Switched to branch '{name}'")
    return 0


async def clone(backend: str, dest: str, name: str | None) -> int:
    target = Path(dest).resolve()
    target.mkdir(parents=True, exist_ok=True)
    cwd = Path.cwd()
    os.chdir(target)
    try:
        repo_name = repo_name_from_env(name or target.name or "repo")
        resolved = backend
        if "://" in resolved or resolved.endswith(".trunk") or "{repo}" in resolved:
            resolved = resolve_backend_url(resolved, repo_name)
        repo = Repository.init(name=repo_name, backend=resolved)
        mirrors = mirror_backends_from_env(repo.name)
        if mirrors:
            repo.set_mirror_urls(mirrors)
        await run_trunk("pull")
        GitCache(repo).rebuild()
        print(f"Cloned into {target}")
        print(f"Repository  {repo.name}")
        print(f"Remote      {repo.backend_url()}")
    finally:
        os.chdir(cwd)
    return 0


async def mount(
    repo_name: str,
    path: str | None,
    *,
    backend: str | None = None,
    require_existing: bool = False,
    mode: str = "disk",
    watch: bool = False,
) -> int:
    if mode == "virtual":
        if watch:
            print("trunks mount: --watch is only valid with --mode disk", file=sys.stderr)
            return 1
        return await _mount_virtual(
            repo_name=repo_name,
            path=path,
            backend=backend,
            require_existing=require_existing,
        )
    target = _mount_path(repo_name, path)
    target.mkdir(parents=True, exist_ok=True)
    try:
        repo = Repository.find(target)
        created = False
    except RepositoryNotFound:
        if require_existing:
            print(f"trunks mount: no Trunks repo at {target}", file=sys.stderr)
            return 1
        resolved_backend = _backend_for_repo_name(repo_name, backend)
        repo = Repository.init(target, name=repo_name, backend=resolved_backend)
        created = True
    if repo.name != repo_name:
        print(f"trunks mount: {target} already contains repo {repo.name!r}, not {repo_name!r}", file=sys.stderr)
        return 1
    if backend:
        resolved_backend = _backend_for_repo_name(repo_name, backend)
        if resolved_backend:
            repo.set_backend_url(resolved_backend)

    cwd = Path.cwd()
    os.chdir(target)
    pulled = False
    try:
        if repo.backend_url():
            async with Trunk(name=repo.name) as trunk:
                await trunk.pull()
            pulled = True
    finally:
        os.chdir(cwd)

    print("Mounted Trunks repository")
    print()
    print(f"Repository  {repo.name}")
    print(f"Path        {target}")
    print(f"Remote      {repo.backend_url() or 'none (local-only)'}")
    print(f"Status      {'created' if created else 'opened'}")
    if pulled:
        print("Sync        pulled")
    if watch:
        runtime = repo_runtime.start(repo)
        print(f"Watcher     running pid {runtime.pid}")
    audit.record(
        repo,
        "mount",
        {"path": str(target), "created": created, "watch": watch, "pulled": pulled},
    )
    _print_shim_hint(repo)
    return 0


async def unmount(path: str | None) -> int:
    target = Path(path or Path.cwd()).expanduser().resolve()
    record = mount_registry.read(target)
    if record is not None:
        return await _unmount_virtual(record)
    try:
        repo = Repository.find(target)
    except RepositoryNotFound:
        print(f"trunks unmount: no Trunks repo at {target}", file=sys.stderr)
        return 1
    runtime = repo_runtime.stop(repo)
    audit.record(repo, "unmount", {"path": str(repo.root), "pid": runtime.pid})
    print("Unmounted Trunks repository")
    print()
    print(f"Repository  {repo.name}")
    print(f"Path        {repo.root}")
    print(f"Watcher     stopped pid {runtime.pid}" if runtime.pid else "Watcher     not running")
    return 0


def _virtual_repo_path(repo_name: str) -> Path:
    home = Path(os.environ.get("TRUNKS_HOME", str(Path.home() / ".trunks")))
    safe = repo_name.replace("/", "_")
    return home / "repos" / safe


async def _mount_virtual(
    *,
    repo_name: str,
    path: str | None,
    backend: str | None,
    require_existing: bool,
) -> int:
    if not path:
        print("trunks mount: --path is required", file=sys.stderr)
        return 1
    target = Path(path).expanduser().resolve()
    await _cleanup_stale_virtual_mount(target)
    if mount_registry.read(target) is not None:
        print(f"trunks mount: {target} is already mounted; run trunks unmount first", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        print(f"trunks mount: {target} is not empty; virtual mounts require an empty mount point", file=sys.stderr)
        return 1

    repo_path = _virtual_repo_path(repo_name)
    repo_path.mkdir(parents=True, exist_ok=True)
    try:
        repo = Repository.find(repo_path)
        created = False
    except RepositoryNotFound:
        if require_existing:
            print(f"trunks mount: no Trunks repo at {repo_path}", file=sys.stderr)
            return 1
        resolved_backend = _backend_for_repo_name(repo_name, backend)
        repo = Repository.init(repo_path, name=repo_name, backend=resolved_backend)
        created = True
    if repo.name != repo_name:
        print(
            f"trunks mount: {repo_path} already contains repo {repo.name!r}, not {repo_name!r}",
            file=sys.stderr,
        )
        return 1
    if backend:
        resolved_backend = _backend_for_repo_name(repo_name, backend)
        if resolved_backend:
            repo.set_backend_url(resolved_backend)

    pulled = False
    if repo.backend_url():
        cwd = Path.cwd()
        os.chdir(repo_path)
        try:
            async with Trunk(name=repo.name) as trunk:
                await trunk.pull()
            pulled = True
        finally:
            os.chdir(cwd)

    runtime_file = repo_path / ".trunks" / "runtime" / "virtual.json"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.unlink(missing_ok=True)

    registry_file = mount_registry.record_path(target)
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    registry_file.unlink(missing_ok=True)

    log_path = repo_path / ".trunks" / "runtime" / "virtual.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    export_name = f"{repo.name.replace('/', '_')}-{ulid().lower()}"
    cmd = [
        sys.executable,
        "-m",
        "trunks.mount.daemon",
        "--repo-path",
        str(repo_path),
        "--target",
        str(target),
        "--export",
        export_name,
        "--runtime-file",
        str(runtime_file),
    ]
    pid = _spawn_detached(cmd, log_path)

    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        if not _pid_exists(pid):
            print(
                "trunks mount: virtual daemon exited before the mount became ready; see logs",
                file=sys.stderr,
            )
            registry_file.unlink(missing_ok=True)
            return 1
        if runtime_file.exists():
            try:
                payload = json.loads(runtime_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                await asyncio.sleep(0.1)
                continue
            record = mount_registry.Record(
                target=target,
                repo_path=repo_path,
                export=payload["export"],
                host=payload["host"],
                port=int(payload["port"]),
                pid=int(payload["pid"]),
            )
            mount_registry.write(record)
            from .mount.lifecycle import (
                MountSandboxedError,
                verify_mount_writable,
            )
            try:
                await verify_mount_writable(target)
            except MountSandboxedError as err:
                await _teardown_failed_virtual_mount(target, record.pid)
                print(str(err), file=sys.stderr)
                return 1
            audit.record(
                repo,
                "mount",
                {"path": str(target), "created": created, "virtual": True, "pulled": pulled},
            )
            print("Mounted Trunks repository")
            print()
            print(f"Repository  {repo.name}")
            print(f"Path        {target}")
            print(f"Repo data   {repo_path}")
            print(f"Remote      {repo.backend_url() or 'none (local-only)'}")
            print(f"NFS         127.0.0.1:{record.port} (export {record.export})")
            print(f"Daemon      pid {record.pid}")
            return 0
        await asyncio.sleep(0.1)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        await _wait_for_pid_exit(pid, timeout=2.0)
    print("trunks mount: virtual daemon did not become ready within 15s", file=sys.stderr)
    return 1


async def _unmount_virtual(record: mount_registry.Record) -> int:
    from .mount.lifecycle import MountError, unmount_nfs

    try:
        await unmount_nfs(record.target, force=True)
    except MountError as err:
        print(f"trunks unmount: {err}", file=sys.stderr)
    try:
        os.kill(record.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        if not await _wait_for_pid_exit(record.pid, timeout=10.0):
            try:
                os.kill(record.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await _wait_for_pid_exit(record.pid, timeout=2.0)
    mount_registry.remove(record.target)
    try:
        repo = Repository.find(record.repo_path)
        audit.record(repo, "unmount", {"path": str(record.target), "pid": record.pid, "virtual": True})
    except RepositoryNotFound:
        pass
    print("Unmounted Trunks repository (virtual)")
    print()
    print(f"Path        {record.target}")
    print(f"Repo data   {record.repo_path}")
    print(f"Daemon      pid {record.pid} (terminated)")
    return 0


async def _cleanup_stale_virtual_mount(target: Path) -> None:
    from .mount.lifecycle import MountError, is_localhost_nfs_mounted, unmount_nfs

    record = mount_registry.read(target)
    if record is None:
        if is_localhost_nfs_mounted(target):
            try:
                await unmount_nfs(target, force=True)
            except MountError:
                pass
        return

    daemon_alive = _pid_exists(record.pid)
    mounted = is_localhost_nfs_mounted(record.target)
    if daemon_alive and mounted and record.target.exists():
        return

    if mounted:
        try:
            await unmount_nfs(record.target, force=True)
        except MountError:
            pass
    if daemon_alive:
        try:
            os.kill(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            if not await _wait_for_pid_exit(record.pid, timeout=3.0):
                try:
                    os.kill(record.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await _wait_for_pid_exit(record.pid, timeout=1.0)
    mount_registry.remove(record.target)


async def _teardown_failed_virtual_mount(target: Path, daemon_pid: int) -> None:
    """Tear down a half-working virtual mount whose probe failed.

    Best-effort: any single step that fails (unmount EPERM, daemon already dead,
    registry already gone) is fine. The point is to leave the system in a state
    where the user can run `trunks mount` again from a non-sandboxed shell.
    """
    from .mount.lifecycle import MountError, unmount_nfs

    try:
        await unmount_nfs(target, force=True)
    except MountError:
        pass
    try:
        os.kill(daemon_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        if not await _wait_for_pid_exit(daemon_pid, timeout=3.0):
            try:
                os.kill(daemon_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await _wait_for_pid_exit(daemon_pid, timeout=1.0)
    mount_registry.remove(target)


async def _wait_for_pid_exit(pid: int, *, timeout: float) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        else:
            if waited_pid == pid:
                return True
        await asyncio.sleep(0.05)
    return False


def _module_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_parent}{os.pathsep}{existing}" if existing else package_parent
    return env


def _spawn_detached(cmd: list[str], log_path: Path) -> int:
    env = _module_subprocess_env()
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        return os.posix_spawn(
            cmd[0],
            cmd,
            env,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, null_fd, 0),
                (os.POSIX_SPAWN_DUP2, log_fd, 1),
                (os.POSIX_SPAWN_DUP2, log_fd, 2),
                (os.POSIX_SPAWN_CLOSE, null_fd),
                (os.POSIX_SPAWN_CLOSE, log_fd),
            ],
            setsid=True,
        )
    finally:
        os.close(null_fd)
        os.close(log_fd)


def _pid_exists(pid: int) -> bool:
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        if waited_pid == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _mount_path(repo_name: str, path: str | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    name = repo_name.rstrip("/").split("/")[-1] or "repo"
    return (Path.cwd() / name).resolve()


def _backend_for_repo_name(repo_name: str, backend: str | None) -> str | None:
    if backend:
        if "://" in backend or backend.endswith(".trunk") or "{repo}" in backend:
            return resolve_backend_url(backend, repo_name)
        return backend
    resolved = backend_from_env(repo_name)
    if resolved:
        return resolved
    if env_forces_local_only():
        return None
    return resolve_backend_url(GlobalConfig.load().default_backend, repo_name)


def init(name: str | None, backend: str | None) -> int:
    repo_name = repo_name_from_env(name or Path.cwd().name or "repo")
    if backend is None:
        backend = backend_from_env(repo_name)
    elif "://" in backend or backend.endswith(".trunk") or "{repo}" in backend:
        backend = resolve_backend_url(backend, repo_name)
    repo = Repository.init(name=repo_name, backend=backend)
    import_existing_git_repository(repo)
    mirrors = mirror_backends_from_env(repo.name)
    if mirrors:
        repo.set_mirror_urls(mirrors)
    GitCache(repo).rebuild()
    print("Initialized Trunks repository")
    print()
    print(f"Repository  {repo.name}")
    print(f"Database    {repo.path.relative_to(Path.cwd()) if repo.path.is_relative_to(Path.cwd()) else repo.path}")
    print(f"Remote      {repo.backend_url() or 'none (local-only)'}")
    if mirrors:
        print(f"Mirrors     {', '.join(mirrors)}")
    _print_shim_hint(repo)
    return 0


def _print_shim_hint(repo: Repository) -> None:
    if not (repo.root / ".git").exists():
        return
    print()
    print("Tip: run `trunks` to open a managed shell where `git push` syncs through Trunks.")


def config(action: str, key: str, value: str | None) -> int:
    cfg = GlobalConfig.load()
    if key != "backend.default":
        print(f"unknown config key: {key}", file=sys.stderr)
        return 1
    if action == "get":
        print(cfg.default_backend or "")
        return 0
    if value is None:
        print("missing value", file=sys.stderr)
        return 1
    GlobalConfig(default_backend=value).save()
    print(f"backend.default={value}")
    return 0


async def status(*, json_output: bool = False, path: str | None = None) -> int:
    repo, virtual_record = _repo_for_status(path)
    state = repo.status(compare_worktree=virtual_record is None)
    display_path = virtual_record.target if virtual_record is not None else repo.root
    if json_output:
        print(json.dumps({
            "repository": repo.name,
            "path": str(display_path),
            "repo_data_path": str(repo.root) if virtual_record is not None else None,
            "virtual": virtual_record is not None,
            "branch": state.branch,
            "head": str(state.head) if state.head else None,
            "backend": state.backend,
            "storage": repo.storage_targets(),
            "dirty": state.dirty,
            "untracked": list(state.untracked),
            "modified": list(state.modified),
            "deleted": list(state.deleted),
            "watcher": _runtime_payload(repo),
        }, sort_keys=True))
        return 0
    print(f"Repository  {repo.name}")
    print(f"Path        {display_path}")
    if virtual_record is not None:
        print(f"Repo data   {repo.root}")
        print("Mount       virtual")
    print(f"Branch      {state.branch}")
    print(f"Remote      {state.backend or 'none (local-only)'}")
    if repo.mirror_targets():
        print(f"Mirrors     {', '.join(f'{name}={url}' for name, url in repo.mirror_targets())}")
    print(f"Status      {'dirty' if state.dirty else 'clean'}")
    if state.untracked:
        print(f"Untracked   {', '.join(state.untracked)}")
    if state.modified:
        print(f"Modified    {', '.join(state.modified)}")
    if state.deleted:
        print(f"Deleted     {', '.join(state.deleted)}")
    runtime = repo_runtime.status(repo)
    print(f"Watcher     {'running pid ' + str(runtime.pid) if runtime.running else 'stopped'}")
    return 0


async def doctor(*, json_output: bool = False, path: str | None = None, ping: bool = False) -> int:
    checks: list[dict[str, object]] = []
    target = Path(path or Path.cwd()).expanduser().resolve()
    repo: Repository | None = None
    try:
        repo = Repository.find(target)
    except RepositoryNotFound as exc:
        checks.append(_doctor_check("repository", False, str(exc)))
    else:
        checks.append(_doctor_check("repository", True, f"{repo.name} at {repo.root}"))

    git = find_system_git()
    checks.append(_doctor_check("git", git is not None, str(git) if git else "not installed; Trunks storage still works without Git"))

    if repo is not None:
        backend = repo.backend_url()
        checks.append(_doctor_check("storage", bool(backend), backend or "none (local-only)"))
        errors = repo.check_integrity()
        checks.append(_doctor_check("integrity", not errors, "ok" if not errors else "; ".join(errors)))
        runtime = repo_runtime.status(repo)
        checks.append(_doctor_check("watcher", True, f"running pid {runtime.pid}" if runtime.running else "stopped"))
        if ping:
            if backend:
                try:
                    await _doctor_ping_storage(repo)
                except Exception as exc:
                    checks.append(_doctor_check("storage_ping", False, str(exc)))
                else:
                    checks.append(_doctor_check("storage_ping", True, "ok"))
            else:
                checks.append(_doctor_check("storage_ping", False, "no storage configured"))

    critical = {"repository", "integrity", "storage_ping"}
    ok = all(bool(check["ok"]) for check in checks if check["name"] in critical)
    if json_output:
        print(json.dumps({"ok": ok, "path": str(target), "checks": checks}, sort_keys=True))
        return 0 if ok else 1

    print("Trunks doctor")
    print()
    for check in checks:
        marker = "ok" if check["ok"] else "fail"
        print(f"{check['name']:<14} {marker:<5} {check['detail']}")
    return 0 if ok else 1


async def _doctor_ping_storage(repo: Repository) -> None:
    targets: list[Storage | str] = []
    primary = repo.primary_storage_profile()
    if primary is not None:
        targets.append(primary)
    elif repo.backend_url():
        targets.append(repo.backend_url() or "")
    targets.extend(repo.mirror_storage_profiles())
    for _, url in repo.mirror_targets():
        if all(not isinstance(target, str) or target != url for target in targets):
            targets.append(url)
    if not targets:
        raise TrunksError("no storage configured")
    for target in targets:
        if isinstance(target, Storage):
            await _ping_storage_profile(target, repo.name)
        else:
            await _ping_storage_url(target)


def _doctor_check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def _runtime_payload(repo: Repository) -> dict[str, object]:
    runtime = repo_runtime.status(repo)
    return {"running": runtime.running, "pid": runtime.pid, "path": str(runtime.path)}


def _repo_for_status(path: str | None) -> tuple[Repository, mount_registry.Record | None]:
    return _resolve_repo(path=Path(path).expanduser().resolve() if path else None)


async def run_trunk(command: str) -> int:
    trunk = Trunk()
    async with trunk:
        if command == "push":
            result = await trunk.push()
            if result is None:
                print("Nothing to push (no remote configured)")
            elif result.refs_pushed == 0 and result.refs_already_current == 0:
                print("Nothing to push (no refs in local trunk — did you commit through the trunks gitshim?)")
            else:
                print(f"Pushed {result.refs_pushed} ref(s), {result.objects_uploaded} object(s); {result.refs_already_current} already current")
        elif command == "pull":
            await trunk.pull()
        elif command == "fetch":
            await trunk.fetch()
    return 0


def check(*, json_output: bool = False, clean: bool = False) -> int:
    repo = _repo_or_init()
    errors = repo.check_integrity()
    if errors:
        if json_output:
            print(json.dumps({"ok": False, "repository": repo.name, "errors": errors}, sort_keys=True))
            return 1
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    GitCache(repo).rebuild()
    removed = repo.clean_unreachable() if clean else 0
    if json_output:
        print(json.dumps({
            "ok": True,
            "repository": repo.name,
            "rebuilt_git_cache": True,
            "removed_unreachable_objects": removed,
        }, sort_keys=True))
        return 0
    print("trunks check: ok")
    print("Git cache   rebuilt")
    if clean:
        print(f"Cleaned     {removed} unreachable objects")
    return 0


def cache(action: str, *, json_output: bool = False, max_size: str = "5GB") -> int:
    manager = CacheManager()
    if action == "stats":
        stats = manager.stats().to_json()
        if json_output:
            print(json.dumps(stats, sort_keys=True))
            return 0
        print(f"Cache       {manager.root}")
        print(f"Objects     {stats['objects']}")
        print(f"Size        {stats['size_bytes']} bytes")
        print(f"Hits        {stats['hits']}")
        print(f"Misses      {stats['misses']}")
        print(f"Hit ratio   {stats['hit_ratio']:.3f}")
        return 0
    if action == "clear":
        manager.clear()
        print(f"Cleared cache at {manager.root}")
        return 0
    if action == "prune":
        removed = manager.prune(max_size_bytes=_parse_size(max_size))
        print(f"Pruned {removed} cache object(s)")
        return 0
    if action == "verify":
        errors = manager.verify()
        if json_output:
            print(json.dumps({"ok": not errors, "errors": errors}, sort_keys=True))
            return 0 if not errors else 1
        if not errors:
            print("trunks cache verify: ok")
            return 0
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("usage: trunks cache [stats|clear|prune|verify]", file=sys.stderr)
    return 1


def _parse_size(value: str) -> int:
    raw = value.strip().lower().replace(" ", "")
    units = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
    for suffix, multiplier in units.items():
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)]) * multiplier)
    return int(raw)


def webhook(
    action: str,
    value: str | None,
    *,
    url: str | None,
    events: str | None,
    json_output: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> int:
    repo = _repo_or_init()
    if action == "create":
        action = "add"
    if action == "list":
        hooks = [_webhook_record(hook.public_record()) for hook in webhooks.list_hooks(repo)]
        if json_output:
            _print_api_list(hooks, limit=limit, offset=offset)
            return 0
        if not hooks:
            print("Webhooks    none")
            return 0
        for hook in hooks:
            print(f"{hook['id']}  {','.join(hook['events'])}  {hook['url']}")
        return 0
    if action == "get":
        if not value:
            print("usage: trunks webhook get <webhook-id> [--json]", file=sys.stderr)
            return 1
        hook = webhooks.get(repo, value)
        if hook is None:
            print(f"trunks webhook: unknown webhook {value}", file=sys.stderr)
            return 1
        record = _webhook_record(hook.public_record())
        if json_output:
            print(json.dumps(record, sort_keys=True))
            return 0
        print(f"{record['id']}  {','.join(record['events'])}  {record['url']}")
        return 0
    if action == "add":
        hook_url = url or value
        if not hook_url:
            print("usage: trunks webhook create <url> [--on push]", file=sys.stderr)
            return 1
        hook = webhooks.add(repo, hook_url, _parse_webhook_events(events) or ["push"])
        if json_output:
            print(json.dumps(_webhook_record(hook.public_record()), sort_keys=True))
        else:
            print(f"Webhook     {hook.id}  {hook.url}")
        return 0
    if action == "update":
        if not value:
            print("usage: trunks webhook update <webhook-id> [--url <url>] [--on push,checkpoint]", file=sys.stderr)
            return 1
        hook = webhooks.update(repo, value, url=url, events=_parse_webhook_events(events))
        if hook is None:
            print(f"trunks webhook: unknown webhook {value}", file=sys.stderr)
            return 1
        record = _webhook_record(hook.public_record())
        if json_output:
            print(json.dumps(record, sort_keys=True))
            return 0
        print(f"Webhook     {hook.id}  {hook.url}")
        return 0
    if action == "delete":
        if not value:
            print("usage: trunks webhook delete <webhook-id>", file=sys.stderr)
            return 1
        if not webhooks.delete(repo, value):
            print(f"trunks webhook: unknown webhook {value}", file=sys.stderr)
            return 1
        if json_output:
            print(json.dumps({"object": "webhook", "id": value, "deleted": True}, sort_keys=True))
            return 0
        print(f"Deleted webhook {value}")
        return 0
    return 1


def audit_cmd(action: str, *, json_output: bool = False, limit: int | None = None, offset: int = 0) -> int:
    repo = _repo_or_init()
    if action != "list":
        print("usage: trunks audit list [--json] [--limit N]", file=sys.stderr)
        return 1
    events = audit.read(repo, limit=None if json_output else limit)
    if json_output:
        records = [_audit_record(event.to_record()) for event in events]
        _print_api_list(records, limit=limit, offset=offset)
        return 0
    if not events:
        print("Audit       no events")
        return 0
    for event in events:
        print(f"{event.timestamp:.3f}  {event.event}  {json.dumps(event.data, sort_keys=True)}")
    return 0


async def log(*, json_output: bool = False) -> int:
    async with Trunk() as trunk:
        async for commit in trunk.log():  # type: ignore[union-attr]
            if json_output:
                print(json.dumps({
                    "id": str(commit.id),
                    "tree": str(commit.tree),
                    "parents": [str(parent) for parent in commit.parents],
                    "author": {"name": commit.author.name, "email": commit.author.email, "when": commit.author.when.isoformat()},
                    "message": commit.message,
                }, sort_keys=True))
            else:
                print(f"{commit.id} {commit.message}")
    return 0


def history(*, json_output: bool = False) -> int:
    repo = _repo_or_init()
    commits: dict[str, dict[str, object]] = {}
    stack = [oid for _, oid in repo.list_refs()]
    while stack:
        oid = stack.pop()
        if str(oid) in commits:
            continue
        commit = repo.load_commit(oid)
        commits[str(oid)] = {
            "id": str(commit.id),
            "tree": str(commit.tree),
            "parents": [str(parent) for parent in commit.parents],
            "message": commit.message,
        }
        stack.extend(commit.parents)
    refs = {name: str(oid) for name, oid in repo.list_refs()}
    payload = {"repository": repo.name, "current_branch": repo.current_branch, "refs": refs, "commits": commits}
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return 0
    for name, oid in refs.items():
        print(f"{name} {oid}")
    return 0


async def storage(
    action: str | None,
    name: str | None,
    url: str | None = None,
    *,
    mirror: bool = False,
    json_output: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> int:
    if action == "list":
        action = None
    if action == "get":
        action = "show"
    if action == "delete":
        action = "remove"
    try:
        repo = Repository.find()
    except RepositoryNotFound:
        print("trunks: no trunks repository here (run `trunks init` first)", file=sys.stderr)
        return 1

    if action is None:
        if json_output:
            _print_api_list([_storage_record(item) for item in repo.storage_targets()], limit=limit, offset=offset)
            return 0
        _print_storage_targets(repo)
        return 0

    if action == "show":
        if not name or url is not None:
            print("usage: trunks storage get <name> [--json]", file=sys.stderr)
            return 1
        return storage_show(repo, name, json_output=json_output)

    if action == "ping":
        return await storage_ping(repo, name)

    if action == "remove":
        if not name or url is not None:
            print("usage: trunks storage delete <name> [--json]", file=sys.stderr)
            return 1
        if not repo.remove_storage(name):
            print(f"storage remove failed: unknown storage target {name}", file=sys.stderr)
            return 1
        remove_global_storage_profile(name)
        if json_output:
            print(json.dumps({"object": "storage_target", "id": name, "name": name, "deleted": True}, sort_keys=True))
            return 0
        _print_storage_targets(repo)
        return 0

    if not name or not url:
        print("usage: trunks storage add <name> <backend-url> [--mirror]", file=sys.stderr)
        return 1
    if not _valid_storage_name(name):
        print(f"storage add failed: invalid storage name {name!r}", file=sys.stderr)
        return 1
    if mirror and name in {"primary", "remote"}:
        print("storage add failed: mirror name cannot be primary", file=sys.stderr)
        return 1
    if _malformed_backend_url(url):
        print(f"storage add failed: malformed backend URL {url}", file=sys.stderr)
        return 1
    if BackendURL.parse(url).scheme == "memory":
        print(f"storage add failed: {url} is ephemeral and cannot be a remote", file=sys.stderr)
        return 1
    resolved_url = resolve_backend_url(url, repo.name)
    if resolved_url is None:
        print(f"storage add failed: {url}", file=sys.stderr)
        return 1
    profile = Storage.from_url(name=name, role="mirror" if mirror else "primary", url=resolved_url)
    backend = backend_from_storage(profile, repo.name)
    if backend is None:
        print(f"storage add failed: {resolved_url}", file=sys.stderr)
        return 1
    async with backend:
        ensure = getattr(backend, "ensure_bucket", None) or getattr(backend, "ensure_container", None)
        if ensure is not None:
            await ensure()
        capabilities = await backend.capabilities()
        if not capabilities.journal or not capabilities.list_prefix or not capabilities.read_after_write_refs:
            print("storage add failed: backend lacks required capabilities", file=sys.stderr)
            return 1
        try:
            await _validate_storage_roundtrip(backend)
        except Exception as exc:
            print(f"storage add failed: {exc}", file=sys.stderr)
            return 1
    _persist_storage_profile(repo, profile)
    await _publish_storage_config(repo)
    if json_output:
        print(json.dumps(_storage_record(profile.public_record(repo.name)), sort_keys=True))
        return 0
    if mirror:
        print(f"Mirror      {name}  {resolved_url}")
    else:
        print(f"Remote      {name}  {resolved_url}")
    return 0


async def storage_from_args(args: argparse.Namespace) -> int:
    action = args.action or "list"
    if action == "wizard":
        return await storage_wizard(args)
    if action in {"create", "update"}:
        action = "add"
    if action == "get":
        action = "show"
    if action == "delete":
        action = "remove"
    if action != "add":
        name = _storage_name_arg(args)
        return await storage(
            None if action == "list" else action,
            name,
            json_output=args.json,
            limit=args.limit,
            offset=args.offset,
        )

    try:
        repo = Repository.find()
    except RepositoryNotFound:
        print("trunks: no trunks repository here (run `trunks init` first)", file=sys.stderr)
        return 1

    try:
        profile, validation_profile = _storage_profiles_from_args(args)
    except ValueError as exc:
        print(f"storage add failed: {exc}", file=sys.stderr)
        return 1

    if not _valid_storage_name(profile.name):
        print(f"storage add failed: invalid storage name {profile.name!r}", file=sys.stderr)
        return 1
    if profile.role == "mirror" and profile.name in {"primary", "remote"}:
        print("storage add failed: mirror name cannot be primary", file=sys.stderr)
        return 1
    if BackendURL.parse(validation_profile.url(repo.name)).scheme == "memory":
        print(f"storage add failed: {validation_profile.url(repo.name)} is ephemeral and cannot be a remote", file=sys.stderr)
        return 1

    try:
        backend = backend_from_storage(validation_profile, repo.name)
    except Exception as exc:
        print(f"storage add failed: {exc}", file=sys.stderr)
        return 1
    if backend is None:
        print(f"storage add failed: {profile.url(repo.name)}", file=sys.stderr)
        return 1
    async with backend:
        ensure = getattr(backend, "ensure_bucket", None) or getattr(backend, "ensure_container", None)
        if ensure is not None:
            await ensure()
        capabilities = await backend.capabilities()
        if not capabilities.journal or not capabilities.list_prefix or not capabilities.read_after_write_refs:
            print("storage add failed: backend lacks required capabilities", file=sys.stderr)
            return 1
        try:
            await _validate_storage_roundtrip(backend)
        except Exception as exc:
            print(f"storage add failed: {exc}", file=sys.stderr)
            return 1

    _persist_storage_profile(repo, profile)
    await _publish_storage_config(repo)
    if getattr(args, "json", False):
        print(json.dumps(_storage_record(profile.public_record(repo.name)), sort_keys=True))
        return 0
    label = "Mirror" if profile.role == "mirror" else "Remote"
    print(f"{label}      {profile.name}  {profile.url(repo.name)}")
    if validation_profile.credentials != profile.credentials or validation_profile.settings != profile.settings:
        print("Credentials validation-only; configure environment variables for future pushes")
    return 0


async def storage_wizard(args: argparse.Namespace) -> int:
    repo = _repo_for_storage_wizard()
    print("Trunks storage wizard")
    print()
    print(f"Repository  {repo.name}")
    print("Configure where this repo pushes and pulls.")
    print()

    name = args.storage_name or _prompt("Storage name", default="primary")
    role = "mirror" if args.mirror else _prompt_choice("Role", ["primary", "mirror"], default="primary")
    backend = args.storage_backend or _prompt_choice(
        "Backend",
        ["local", "s3", "r2", "tigris", "gcs", "azure", "sftp", "postgres"],
        default="local",
    )
    values = _wizard_storage_values(backend, repo.name)
    namespace = argparse.Namespace(
        values=[],
        storage_name=name,
        storage_url=None,
        storage_backend=backend,
        mirror=role == "mirror",
        bucket=values.get("bucket"),
        region=values.get("region"),
        endpoint=values.get("endpoint"),
        prefix=values.get("prefix"),
        account_id=values.get("account_id"),
        access_key=values.get("access_key"),
        secret_key=values.get("secret_key"),
        session_token=values.get("session_token"),
        path=values.get("path"),
        host=values.get("host"),
        port=values.get("port"),
        user=values.get("user"),
        password=values.get("password"),
        password_env=values.get("password_env"),
        ssh_key=values.get("ssh_key"),
        key_passphrase=values.get("key_passphrase"),
        dsn=values.get("dsn"),
        dsn_env=values.get("dsn_env"),
        container=values.get("container"),
        account_name=values.get("account_name"),
        account_key=values.get("account_key"),
        sas_token=values.get("sas_token"),
        service_account_file=values.get("service_account_file"),
        project=values.get("project"),
    )
    print()
    return await storage_from_args(argparse.Namespace(**vars(namespace), action="add"))


def _repo_for_storage_wizard() -> Repository:
    try:
        return Repository.find()
    except RepositoryNotFound:
        if not _prompt_yes_no("No Trunks repo found here. Create one in this directory?", default=True):
            raise
        return Repository.init(name=repo_name_from_env(Path.cwd().name or "repo"), backend=None)


def _wizard_storage_values(backend: str, repo_name: str) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    if backend in {"local", "file", "nfs", "smb", "nas"}:
        values["path"] = _prompt("Storage path", default=str(Path.home() / ".trunks" / "store"))
        return values
    if backend in {"s3", "r2", "tigris", "b2", "wasabi", "spaces", "ceph", "netapp", "gcs-s3"}:
        values["bucket"] = _prompt("Bucket", required=True)
        values["prefix"] = _prompt("Prefix", default="trunks/{repo}.trunk").replace("{repo}", repo_name)
        values["region"] = _prompt("Region", default="").strip() or None
        values["endpoint"] = _prompt("Endpoint URL", default="").strip() or None
        if _prompt_yes_no("Store access key in local Trunks config?", default=False):
            values["access_key"] = _prompt("Access key", required=True)
            values["secret_key"] = _prompt("Secret key", required=True, secret=True)
        else:
            print("Set credentials with environment variables or your cloud SDK before push/pull.")
        return values
    if backend == "gcs":
        values["bucket"] = _prompt("Bucket", required=True)
        values["prefix"] = _prompt("Prefix", default="trunks/{repo}.trunk").replace("{repo}", repo_name)
        values["project"] = _prompt("Project", default="").strip() or None
        values["service_account_file"] = _prompt("Service account file", default="").strip() or None
        return values
    if backend == "azure":
        values["account_name"] = _prompt("Account name", required=True)
        values["container"] = _prompt("Container", required=True)
        values["prefix"] = _prompt("Prefix", default="trunks/{repo}.trunk").replace("{repo}", repo_name)
        if _prompt_yes_no("Store account key or SAS token in local Trunks config?", default=False):
            values["account_key"] = _prompt("Account key", default="", secret=True).strip() or None
            values["sas_token"] = _prompt("SAS token", default="", secret=True).strip() or None
        return values
    if backend == "sftp":
        values["host"] = _prompt("Host", required=True)
        values["port"] = _prompt("Port", default="").strip() or None
        values["user"] = _prompt("User", default="").strip() or None
        values["path"] = _prompt("Remote path", required=True)
        auth = _prompt_choice("Auth", ["ssh-key", "password-env"], default="ssh-key")
        if auth == "ssh-key":
            values["ssh_key"] = _prompt("SSH key path", required=True)
            passphrase = _prompt("SSH key passphrase", default="", secret=True)
            values["key_passphrase"] = passphrase or None
        else:
            values["password_env"] = _prompt("Password environment variable", default="TRUNKS_SFTP_PASSWORD")
        return values
    if backend == "postgres":
        values["dsn_env"] = _prompt("DSN environment variable", default="TRUNKS_POSTGRES_DSN")
        dsn = _prompt("Validation DSN", default="", secret=True).strip()
        values["dsn"] = dsn or None
        return values
    raise ValueError(f"unsupported backend: {backend}")


def _prompt(label: str, *, default: str | None = None, required: bool = False, secret: bool = False) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = getpass(f"{label}{suffix}: ") if secret else input(f"{label}{suffix}: ")
        value = raw.strip() or (default if default is not None else "")
        if value or not required:
            return value
        print(f"{label} is required.")


def _prompt_choice(label: str, choices: list[str], *, default: str) -> str:
    shown = "/".join(choice.upper() if choice == default else choice for choice in choices)
    while True:
        value = _prompt(f"{label} ({shown})", default=default)
        if value in choices:
            return value
        print(f"Choose one of: {', '.join(choices)}")


def _prompt_yes_no(label: str, *, default: bool) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{default_label}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter yes or no.")


def _storage_name_arg(args: argparse.Namespace) -> str | None:
    if args.storage_name:
        return args.storage_name
    return args.values[0] if args.values else None


def _storage_profiles_from_args(args: argparse.Namespace) -> tuple[Storage, Storage]:
    role = "mirror" if args.mirror else "primary"
    storage_url = getattr(args, "storage_url", None)
    if storage_url and args.values:
        raise ValueError("use either --url or positional URL, not both")
    if args.values and len(args.values) not in {1, 2}:
        raise ValueError("usage: trunks storage add [--name <name>] <url> or trunks storage add --name <name> --backend <type> ...")
    if args.storage_backend is None:
        if storage_url:
            if not args.storage_name:
                raise ValueError("--name is required with --url")
            name, url = args.storage_name, storage_url
        elif len(args.values) == 2:
            name, url = args.values
        elif len(args.values) == 1 and args.storage_name:
            name, url = args.storage_name, args.values[0]
        else:
            raise ValueError("usage: trunks storage add <name> <backend-url> [--mirror]")
        if _malformed_backend_url(url):
            raise ValueError(f"malformed backend URL {url}")
        resolved = resolve_backend_url(url, Repository.find().name)
        if not resolved:
            raise ValueError(url)
        profile = Storage.from_url(name=name, role=role, url=resolved)
        return profile, profile

    if not args.storage_name:
        raise ValueError("--name is required with --backend")
    backend = args.storage_backend
    settings, credentials, validation_credentials = _storage_fields_from_args(args)
    validation_settings = dict(settings)
    if backend == "postgres":
        if args.dsn_env:
            settings["dsn_env"] = args.dsn_env
        if args.dsn:
            validation_settings["dsn"] = args.dsn
        if not args.dsn and not args.dsn_env:
            raise ValueError("--dsn-env is required for postgres storage (or pass --dsn for one-time validation)")
        if args.dsn and not args.dsn_env:
            raise ValueError("--dsn is validation-only; pass --dsn-env so future pushes can read the DSN from the environment")
    profile = Storage(name=args.storage_name, backend=backend, role=role, settings=settings, credentials=credentials)
    validation_profile = Storage(
        name=args.storage_name,
        backend=backend,
        role=role,
        settings=validation_settings,
        credentials={**credentials, **validation_credentials},
    )
    return profile, validation_profile


def _storage_fields_from_args(args: argparse.Namespace) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    settings: dict[str, str] = {}
    credentials: dict[str, str] = {}
    validation_credentials: dict[str, str] = {}
    for attr, key in [
        ("bucket", "bucket"),
        ("region", "region"),
        ("endpoint", "endpoint"),
        ("prefix", "prefix"),
        ("account_id", "account_id"),
        ("path", "path"),
        ("host", "host"),
        ("port", "port"),
        ("user", "user"),
        ("container", "container"),
        ("account_name", "account_name"),
        ("project", "project"),
    ]:
        value = getattr(args, attr, None)
        if value:
            settings[key] = value
    for attr, key in [
        ("password_env", "password_env"),
        ("ssh_key", "ssh_key"),
        ("key_passphrase", "key_passphrase"),
        ("service_account_file", "service_account_file"),
    ]:
        value = getattr(args, attr, None)
        if value:
            credentials[key] = value
    for attr, key in [
        ("access_key", "access_key"),
        ("secret_key", "secret_key"),
        ("session_token", "session_token"),
        ("password", "password"),
        ("account_key", "account_key"),
        ("sas_token", "sas_token"),
    ]:
        value = getattr(args, attr, None)
        if value:
            credentials[key] = value
    return settings, credentials, validation_credentials


def _persist_storage_profile(repo: Repository, profile: Storage) -> None:
    set_global_storage_profile(profile)
    repo.set_storage_profile(_repo_storage_profile(profile))


def _repo_storage_profile(profile: Storage) -> Storage:
    return Storage(
        name=profile.name,
        backend=profile.backend,
        role=profile.role,
        settings=dict(profile.settings),
        credentials={},
    )


def _effective_storage_profile(profile: Storage) -> Storage:
    global_profile = get_global_storage_profile(profile.name)
    if global_profile is None:
        return profile
    return Storage(
        name=profile.name,
        backend=profile.backend,
        role=profile.role,
        settings={**global_profile.settings, **profile.settings},
        credentials={**global_profile.credentials, **profile.credentials},
    )


def storage_show(repo: Repository, name: str, *, json_output: bool = False) -> int:
    profile = repo.storage_profile(name)
    if profile is None:
        print(f"storage show failed: unknown storage target {name}", file=sys.stderr)
        return 1
    effective = _effective_storage_profile(profile)
    if json_output:
        print(json.dumps(_storage_record(profile.public_record(repo.name)), sort_keys=True))
        return 0
    record = effective.masked_record()
    print(f"name        {record['name']}")
    print(f"role        {record['role']}")
    print(f"backend     {record['backend']}")
    print(f"url         {profile.url(repo.name)}")
    for group in ("settings", "credentials"):
        values = record[group]
        if isinstance(values, dict):
            for key in sorted(values):
                print(f"{key:<11} {values[key]}")
    return 0


async def storage_ping(repo: Repository, target: str | None) -> int:
    targets: list[tuple[str, str, Storage | None]] = []
    if target in {None, "all"}:
        primary = repo.primary_storage_profile()
        if primary is not None:
            targets.append((primary.name, primary.url(repo.name), primary))
        elif repo.backend_url():
            targets.append((repo.primary_storage_name(), repo.backend_url() or "", None))
        for profile in repo.mirror_storage_profiles():
            targets.append((profile.name, profile.url(repo.name), profile))
    elif target in {"primary", "remote"}:
        primary = repo.primary_storage_profile()
        if primary is not None:
            targets.append((primary.name, primary.url(repo.name), primary))
        elif repo.backend_url():
            targets.append((repo.primary_storage_name(), repo.backend_url() or "", None))
    elif target in {"mirror", "mirrors"}:
        for profile in repo.mirror_storage_profiles():
            targets.append((profile.name, profile.url(repo.name), profile))
    else:
        profile = repo.storage_profile(target)
        resolved = profile.url(repo.name) if profile is not None else repo.storage_url(target)
        if resolved is None:
            if not _looks_like_backend_locator(target):
                print(f"storage ping failed: unknown storage target {target}", file=sys.stderr)
                return 1
            resolved = resolve_backend_url(target, repo.name)
        if resolved is None:
            print(f"storage ping failed: {target}", file=sys.stderr)
            return 1
        targets.append((target, resolved, profile))

    if not targets:
        print("storage ping failed: no storage configured", file=sys.stderr)
        return 1

    ok = True
    for label, target, profile in targets:
        try:
            if profile is None:
                await _ping_storage_url(target)
            else:
                await _ping_storage_profile(profile, repo.name)
        except Exception as exc:
            ok = False
            print(f"{label}      failed  {target}  {exc}", file=sys.stderr)
        else:
            print(f"{label}      ok      {target}")
    return 0 if ok else 1


def _print_storage_targets(repo: Repository) -> None:
    profiles = repo.list_storage_profiles()
    if profiles:
        for profile in profiles:
            print(f"{profile.role:<11} {profile.name}  {profile.url(repo.name)}")
        return
    if not repo.backend_url() and not repo.mirror_targets():
        print("Storage     none (local-only)")
        return
    if repo.backend_url():
        print(f"primary     {repo.primary_storage_name()}  {repo.backend_url()}")
    for name, url in repo.mirror_targets():
        print(f"mirror      {name}  {url}")


def _valid_storage_name(value: str) -> bool:
    if value in {"all", "mirror", "mirrors", "remote"}:
        return False
    return bool(value) and all(ch.isalnum() or ch in {"-", "_", "."} for ch in value)


def _looks_like_backend_locator(value: str) -> bool:
    return "://" in value or "/" in value or value.endswith(".trunk")


def _malformed_backend_url(value: str) -> bool:
    return ":" in value and "://" not in value


async def _ping_storage_url(url: str) -> None:
    if BackendURL.parse(url).scheme == "memory":
        raise TrunksError("memory:// is ephemeral and cannot be remote storage")
    backend = backend_from_url(url)
    if backend is None:
        raise TrunksError("unsupported backend")
    async with backend:
        ensure = getattr(backend, "ensure_bucket", None) or getattr(backend, "ensure_container", None)
        if ensure is not None:
            await ensure()
        capabilities = await backend.capabilities()
        if not capabilities.journal or not capabilities.list_prefix or not capabilities.read_after_write_refs:
            raise TrunksError("backend lacks required capabilities")
        await _validate_storage_roundtrip(backend)


async def _ping_storage_profile(profile: Storage, repo_name: str) -> None:
    if BackendURL.parse(profile.url(repo_name)).scheme == "memory":
        raise TrunksError("memory:// is ephemeral and cannot be remote storage")
    backend = backend_from_storage(profile, repo_name)
    if backend is None:
        raise TrunksError("unsupported backend")
    async with backend:
        ensure = getattr(backend, "ensure_bucket", None) or getattr(backend, "ensure_container", None)
        if ensure is not None:
            await ensure()
        capabilities = await backend.capabilities()
        if not capabilities.journal or not capabilities.list_prefix or not capabilities.read_after_write_refs:
            raise TrunksError("backend lacks required capabilities")
        await _validate_storage_roundtrip(backend)


async def _publish_storage_config(repo: Repository) -> None:
    if not repo.backend_url():
        return
    primary_profile = repo.primary_storage_profile()
    primary = backend_from_storage(primary_profile, repo.name) if primary_profile is not None else backend_from_url(repo.backend_url())
    if primary is None:
        return
    mirror_profiles = repo.mirror_storage_profiles()
    mirrors = [backend_from_storage(profile, repo.name) for profile in mirror_profiles]
    if not mirror_profiles:
        mirrors = [backend_from_url(url) for url in repo.mirror_urls()]
    mirrors = [backend for backend in mirrors if backend is not None]
    if mirrors:
        from .backends.multi import Multi

        backend = Multi(primary=primary, mirrors=mirrors)
    else:
        backend = primary
    async with backend:
        await backend.write_config(repo.storage_config())


async def _validate_storage_roundtrip(backend) -> None:
    token = ulid()
    blob = Blob.from_data(f"trunks storage validation {token}\n".encode())
    await backend.write_object(blob.id, blob.canonical())
    if await backend.read_object(blob.id) != blob.canonical():
        raise TrunksError("object read-after-write returned different bytes")
    refs = []
    async for ref in backend.list_refs("refs/"):
        refs.append(ref)
        if len(refs) > 1:
            break
    journal = JournalEntry.create("storage-validation", {"token": token, "object": str(blob.id)}, id=token)
    await backend.append_journal(journal)


def shell(backend: str | None) -> int:
    if find_system_git() is None:
        print(GIT_NOT_FOUND_MESSAGE, file=sys.stderr)
        return 1
    repo = _repo_or_init(backend=backend)
    GitCache(repo).rebuild()
    print("Welcome to Trunks 0.1")
    print()
    print(f"Repository  {repo.name}")
    print(f"Branch      {repo.current_branch}")
    print(f"Remote      {repo.backend_url() or 'none (local-only)'}")
    if repo.mirror_urls():
        print(f"Mirrors     {', '.join(repo.mirror_urls())}")
    print()
    print("Use git normally. Type exit to leave Trunks.")
    shim_dir = Path(tempfile.mkdtemp(prefix="trunks-git-"))
    git_path = shim_dir / "git"
    git_path.write_text(
        f"#!{sys.executable}\nfrom trunks.gitshim import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    git_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    env["TRUNKS_ACTIVE"] = "1"
    env["TRUNKS_GIT_SHIM_DIR"] = str(shim_dir)
    shell_path = os.environ.get("SHELL") or shutil.which("sh") or "/bin/sh"
    try:
        return subprocess.call([shell_path], cwd=repo.root, env=env)
    finally:
        shutil.rmtree(shim_dir, ignore_errors=True)


def _shim_script() -> str:
    # Bake the import path that this CLI process uses so dev checkouts, venvs,
    # and user-site installs (~/Library/Python/.../site-packages) keep working
    # when the shim is invoked from a subprocess with no PYTHONPATH and a
    # different HOME. We capture site.getsitepackages() and the user-site so
    # third-party deps (orjson, uvloop) resolve regardless of how the caller's
    # environment is shaped.
    import site

    package_parent = Path(__file__).resolve().parent.parent
    extra: list[str] = [str(package_parent)]
    user_site = site.getusersitepackages()
    if user_site:
        extra.append(user_site)
    for entry in site.getsitepackages():
        if entry and entry not in extra:
            extra.append(entry)
    baked = os.pathsep.join(extra)
    return (
        "#!/usr/bin/env bash\n"
        f'export TRUNKS_GIT_SHIM_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        f'export PYTHONPATH="{baked}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        f'exec "{sys.executable}" -m trunks.gitshim "$@"\n'
    )


def shim(action: str, dest: str | None) -> int:
    if action == "print":
        sys.stdout.write(_shim_script())
        return 0
    target_dir = Path(dest) if dest else Path.home() / ".local" / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "git"
    if action == "path":
        print(target)
        return 0
    if target.exists() and target.read_text(encoding="utf-8") != _shim_script():
        backup = target.with_suffix(".bak")
        target.rename(backup)
        print(f"backed up existing {target} -> {backup}", file=sys.stderr)
    target.write_text(_shim_script(), encoding="utf-8")
    target.chmod(0o755)
    _write_shim_marker()
    print(f"installed git shim -> {target}")
    print(f"add to PATH:  export PATH=\"{target_dir}:$PATH\"")
    return 0


def _write_shim_marker() -> None:
    from .gitcache import _shim_marker_path
    marker = _shim_marker_path()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1\n", encoding="utf-8")
    except OSError:
        pass


def _resolve_repo(
    *,
    path: Path | None = None,
    backend: str | None = None,
) -> tuple[Repository, mount_registry.Record | None]:
    """Resolve the repo for a CLI command, honoring active virtual mounts.

    Returns the backing Repository plus the registry Record when the path falls
    inside a virtual mount target. When `path` is None, behaves like the legacy
    `_repo_or_init`: finds-or-inits a repo at cwd. When `path` is set, only
    finds — never inits — at the given path.
    """
    target = (path or Path.cwd()).expanduser().resolve()
    record = mount_registry.find_for_path(target)
    if record is not None:
        return Repository.find(record.repo_path), record
    if path is not None:
        return Repository.find(target), None
    try:
        repo = Repository.find()
    except RepositoryNotFound:
        repo_name = Path.cwd().name or "repo"
        repo_name = repo_name_from_env(repo_name)
        if backend is None:
            backend = backend_from_env(repo_name)
        if backend is None and not env_forces_local_only():
            backend = resolve_backend_url(GlobalConfig.load().default_backend, repo_name)
        repo = Repository.init(name=repo_name, backend=backend)
        mirrors = mirror_backends_from_env(repo_name)
        if mirrors:
            repo.set_mirror_urls(mirrors)
    if backend:
        repo.set_backend_url(backend)
    env_mirrors = mirror_backends_from_env(repo.name)
    if env_mirrors:
        repo.set_mirror_urls(env_mirrors)
    return repo, None


def _repo_or_init(backend: str | None = None) -> Repository:
    repo, _ = _resolve_repo(backend=backend)
    return repo


if __name__ == "__main__":
    main()
