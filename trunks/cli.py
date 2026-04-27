from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import (
    GlobalConfig,
    backend_from_env,
    env_forces_local_only,
    mirror_backends_from_env,
    mirror_policy_from_env,
    push_mode_from_env,
    repo_name_from_env,
    resolve_backend_url,
)
from .errors import RepositoryNotFound, TrunksError
from .gitcache import GIT_NOT_FOUND_MESSAGE, GitCache, find_system_git
from .gitimport import import_existing_git_repository
from .journal import JournalEntry
from .objects import Blob
from .repository import Repository
from .storage import Storage
from .trunk import Trunk
from .ids import ulid
from .url import BackendURL, backend_from_storage, backend_from_url


def main() -> None:
    raise SystemExit(asyncio.run(dispatch(sys.argv[1:])))


async def dispatch(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="trunks")
    parser.add_argument("--backend", default=None)
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init")
    init_p.add_argument("--backend", default=None)
    init_p.add_argument("--name", default=None)

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
    sub.add_parser("push")
    sub.add_parser("pull")
    sub.add_parser("fetch")
    check_p = sub.add_parser("check")
    check_p.add_argument("--json", action="store_true")
    check_p.add_argument("--clean", action="store_true")
    storage_p = sub.add_parser("storage")
    storage_p.add_argument("action", nargs="?", choices=["add", "remove", "ping", "list", "show"])
    storage_p.add_argument("values", nargs="*")
    storage_p.add_argument("--name", dest="storage_name", default=None)
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
    log_p = sub.add_parser("log")
    log_p.add_argument("--json", action="store_true")

    add_p = sub.add_parser("add", help="stage paths into trunks (also stages in .git)")
    add_p.add_argument("paths", nargs="*")
    add_p.add_argument("-f", "--force", action="store_true")

    commit_p = sub.add_parser("commit", help="record a commit in trunks")
    commit_p.add_argument("-m", "--message", default="")
    commit_p.add_argument("-a", "--all", action="store_true", help="stage tracked changes first")

    branch_p = sub.add_parser("branch", help="list or create a branch")
    branch_p.add_argument("name", nargs="?")
    branch_p.add_argument("-d", "--delete", default=None, metavar="NAME")

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
        if args.command == "clone":
            return await clone(args.backend, args.dest, args.name)
        if args.command == "config":
            return config(args.action, args.key, args.value)
        if args.command == "status":
            return await status(json_output=args.json)
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
        if args.command == "log":
            return await log(json_output=args.json)
        if args.command == "add":
            return cmd_add(args.paths, args.force)
        if args.command == "commit":
            return cmd_commit(args.message, args.all)
        if args.command == "branch":
            return cmd_branch(args.name, args.delete)
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


def cmd_branch(name: str | None, delete: str | None) -> int:
    repo = _repo_or_init()
    if delete:
        if delete == repo.current_branch:
            print(f"trunks: cannot delete current branch '{delete}'", file=sys.stderr)
            return 1
        repo.delete_ref(delete)
        GitCache(repo).rebuild()
        print(f"Deleted branch {delete}")
        return 0
    if name:
        repo.create_branch(name, switch=False)
        GitCache(repo).rebuild()
        print(f"Created branch {name}")
        return 0
    current = repo.current_branch
    for ref_name, _ in repo.list_refs():
        branch = ref_name.removeprefix("refs/heads/")
        marker = "*" if branch == current else " "
        print(f"{marker} {branch}")
    return 0


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
    return 0


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


async def status(*, json_output: bool = False) -> int:
    repo = _repo_or_init()
    state = repo.status(compare_worktree=True)
    if json_output:
        print(json.dumps({
            "repository": repo.name,
            "branch": state.branch,
            "head": str(state.head) if state.head else None,
            "backend": state.backend,
            "storage": repo.storage_targets(),
            "dirty": state.dirty,
        }, sort_keys=True))
        return 0
    print(f"Repository  {repo.name}")
    print(f"Branch      {state.branch}")
    print(f"Remote      {state.backend or 'none (local-only)'}")
    if repo.mirror_targets():
        print(f"Mirrors     {', '.join(f'{name}={url}' for name, url in repo.mirror_targets())}")
    print(f"Status      {'dirty' if state.dirty else 'clean'}")
    return 0


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


async def storage(action: str | None, name: str | None, url: str | None = None, *, mirror: bool = False) -> int:
    if action == "list":
        action = None
    try:
        repo = Repository.find()
    except RepositoryNotFound:
        print("trunks: no trunks repository here (run `trunks init` first)", file=sys.stderr)
        return 1

    if action is None:
        _print_storage_targets(repo)
        return 0

    if action == "show":
        if not name or url is not None:
            print("usage: trunks storage show <name>", file=sys.stderr)
            return 1
        return storage_show(repo, name)

    if action == "ping":
        return await storage_ping(repo, name)

    if action == "remove":
        if not name or url is not None:
            print("usage: trunks storage remove <name>", file=sys.stderr)
            return 1
        if not repo.remove_storage(name):
            print(f"storage remove failed: unknown storage target {name}", file=sys.stderr)
            return 1
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
    if mirror:
        repo.set_storage_profile(profile)
        await _publish_storage_config(repo)
        print(f"Mirror      {name}  {resolved_url}")
    else:
        repo.set_storage_profile(profile)
        await _publish_storage_config(repo)
        print(f"Remote      {name}  {resolved_url}")
    return 0


async def storage_from_args(args: argparse.Namespace) -> int:
    action = args.action or "list"
    if action != "add":
        name = _storage_name_arg(args)
        return await storage(None if action == "list" else action, name)

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

    repo.set_storage_profile(profile)
    await _publish_storage_config(repo)
    label = "Mirror" if profile.role == "mirror" else "Remote"
    print(f"{label}      {profile.name}  {profile.url(repo.name)}")
    if validation_profile.credentials != profile.credentials or validation_profile.settings != profile.settings:
        print("Credentials validation-only; configure environment variables for future pushes")
    return 0


def _storage_name_arg(args: argparse.Namespace) -> str | None:
    if args.storage_name:
        return args.storage_name
    return args.values[0] if args.values else None


def _storage_profiles_from_args(args: argparse.Namespace) -> tuple[Storage, Storage]:
    role = "mirror" if args.mirror else "primary"
    if args.values and len(args.values) not in {1, 2}:
        raise ValueError("usage: trunks storage add [--name <name>] <url> or trunks storage add --name <name> --backend <type> ...")
    if args.storage_backend is None:
        if len(args.values) == 2:
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


def storage_show(repo: Repository, name: str) -> int:
    profile = repo.storage_profile(name)
    if profile is None:
        print(f"storage show failed: unknown storage target {name}", file=sys.stderr)
        return 1
    record = profile.masked_record()
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
    return (
        "#!/usr/bin/env bash\n"
        f'export TRUNKS_GIT_SHIM_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
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
    print(f"installed git shim -> {target}")
    print(f"add to PATH:  export PATH=\"{target_dir}:$PATH\"")
    return 0


def _repo_or_init(backend: str | None = None) -> Repository:
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
    return repo


if __name__ == "__main__":
    main()
