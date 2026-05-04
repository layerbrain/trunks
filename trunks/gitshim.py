from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
import zlib
from pathlib import Path

from .config import mirror_policy_from_env, push_mode_from_env
from .errors import InvalidPath, RefConflict, RepositoryNotFound
from .gitcache import GitCache, is_foreign_git_dir, is_trunks_managed_git_dir, system_git
from .ids import ObjectId
from .repository import Repository


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    original_argv = list(argv)
    if _env_flag("TRUNKS_BYPASS"):
        return _passthrough(argv)
    if not argv:
        return _passthrough(argv)
    leading, argv = _split_global_options(argv)
    if _uses_explicit_git_location(leading):
        return _passthrough(original_argv)
    try:
        repo = Repository.find()
    except (RepositoryNotFound, OSError):
        return _passthrough(original_argv)
    if not _should_handle(repo):
        return _passthrough(original_argv)

    if not argv:
        return _passthrough(leading)
    command = argv[0]
    args = argv[1:]
    try:
        if command == "status":
            return _status(repo)
        if command == "init":
            GitCache(repo).rebuild(force=True)
            print("Reinitialized existing Trunks repository")
            return 0
        if command == "add":
            return _add(repo, args)
        if command == "commit":
            return _commit(repo, args)
        if command in {"checkout", "switch"}:
            return _checkout(repo, args)
        if command == "branch":
            return _branch(repo, args)
        if command == "push":
            return _push(repo, args)
        if command == "fetch":
            return _trunks_command("fetch")
        if command == "pull":
            code = _trunks_command("pull")
            return code
        if command == "ls-files":
            return _ls_files(repo)
        if command == "log":
            return _log(repo, args)
        if command == "rm":
            return _rm(repo, args)
        if command == "mv":
            return _mv(repo, args)
        if command == "restore":
            return _restore(repo, args)
        if command == "diff":
            return _diff(repo, args)
        if command == "show":
            return _show(repo, args)
        if command == "tag":
            return _tag(repo, args)
        if command == "remote":
            return _remote(repo, args)
        if command in {"merge", "rebase", "cherry-pick"}:
            return _working_tree_git(repo, [*leading, *argv])
        if command in {"config", "help", "version"}:
            return _passthrough([*leading, *argv])
        if command in {"worktree", "submodule", "lfs", "notes", "reflog", "bisect"}:
            sys.stderr.write(f"git: '{command}' is not supported by Trunks v1\n")
            return 1
    except RefConflict as exc:
        sys.stderr.write(f"! [rejected] {exc}\n")
        return 1
    except InvalidPath as exc:
        sys.stderr.write(f"fatal: {exc}\n")
        return 128
    return _passthrough([*leading, *argv])


def _split_global_options(argv: list[str]) -> tuple[list[str], list[str]]:
    leading: list[str] = []
    rest = list(argv)
    takes_value = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    while rest:
        head = rest[0]
        if head in takes_value:
            if len(rest) >= 2:
                leading.extend(rest[:2])
                rest = rest[2:]
                continue
            leading.append(rest.pop(0))
            continue
        if head.startswith("--git-dir=") or head.startswith("--work-tree=") or head.startswith("--namespace=") or head.startswith("-C="):
            leading.append(rest.pop(0))
            continue
        if head in {"--bare", "--no-pager", "--no-replace-objects", "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "-p", "--paginate"}:
            leading.append(rest.pop(0))
            continue
        break
    return leading, rest


def _env_flag(name: str) -> bool:
    return os.environ.get(name) in {"1", "true", "yes", "on"}


def _uses_explicit_git_location(leading: list[str]) -> bool:
    return any(
        option == "-C"
        or option.startswith("-C=")
        or option == "--git-dir"
        or option.startswith("--git-dir=")
        or option == "--work-tree"
        or option.startswith("--work-tree=")
        for option in leading
    )


def _should_handle(repo: Repository) -> bool:
    git_dir = repo.root / ".git"
    if is_foreign_git_dir(git_dir):
        return False
    return _env_flag("TRUNKS_ACTIVE") or is_trunks_managed_git_dir(git_dir)


def _status(repo: Repository) -> int:
    status = repo.status(compare_worktree=True)
    print(f"On branch {status.branch}")
    if status.dirty:
        print("Changes not staged for commit")
    else:
        print("nothing to commit, working tree clean")
    return 0


def _add(repo: Repository, args: list[str]) -> int:
    force, targets = _parse_add_args(args)
    targets = targets or ["."]
    for target in targets:
        path = _worktree_path(repo, target)
        repo.add_worktree_path(path, force=force)
    GitCache(repo).rebuild(force=True)
    entries = repo.index_entries()
    if not entries:
        return 0
    result = subprocess.run(
        [system_git(), "add", "-f", "--", *(entry.path for entry in entries)],
        cwd=repo.root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_system_git_env(),
        check=False,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def _parse_add_args(args: list[str]) -> tuple[bool, list[str]]:
    force = False
    targets: list[str] = []
    parse_options = True
    for arg in args:
        if parse_options and arg == "--":
            parse_options = False
            continue
        if parse_options and arg in {"-f", "--force"}:
            force = True
            continue
        if parse_options and arg in {"-A", "--all"}:
            targets.append(".")
            continue
        if parse_options and arg.startswith("-"):
            raise InvalidPath(f"unsupported git add option: {arg}")
        targets.append(arg)
    return force, targets


def _commit(repo: Repository, args: list[str]) -> int:
    if (repo.root / ".git" / "MERGE_HEAD").exists() or (repo.root / ".git" / "CHERRY_PICK_HEAD").exists():
        return _working_tree_git(repo, ["commit", *args])
    parser = argparse.ArgumentParser(prog="git commit", add_help=False)
    parser.add_argument("-m", "--message", default="")
    parsed, _ = parser.parse_known_args(args)
    message = parsed.message or "commit"
    commit = repo.create_commit(message=message)
    GitCache(repo).rebuild(force=True)
    print(f"[{repo.current_branch} {commit.id.short(7)}] {message}")
    return 0


def _checkout(repo: Repository, args: list[str]) -> int:
    if not args:
        sys.stderr.write("git checkout: missing branch\n")
        return 1
    if args[0] in {"-b", "-c"}:
        if len(args) < 2:
            sys.stderr.write(f"git checkout {args[0]}: missing branch\n")
            return 1
        repo.create_branch(args[1])
        GitCache(repo).rebuild(force=True)
        print(f"Switched to a new branch '{args[1]}'")
        return 0
    repo.checkout(args[0])
    GitCache(repo).rebuild(force=True)
    print(f"Switched to branch '{args[0]}'")
    return 0


def _branch(repo: Repository, args: list[str]) -> int:
    if args and args[0] == "-d":
        if len(args) < 2:
            sys.stderr.write("git branch -d: missing branch\n")
            return 1
        if args[1] == repo.current_branch:
            sys.stderr.write(f"error: cannot delete branch '{args[1]}' checked out at '{repo.root}'\n")
            return 1
        repo.delete_ref(args[1])
        GitCache(repo).rebuild(force=True)
        print(f"Deleted branch {args[1]}")
        return 0
    if args and args[0].startswith("-"):
        GitCache(repo).rebuild(force=True)
        return _passthrough(["branch", *args])
    if args:
        repo.create_branch(args[0], switch=False)
        GitCache(repo).rebuild(force=True)
        return 0
    current = repo.current_branch
    for name, _ in repo.list_refs():
        branch = name.removeprefix("refs/heads/")
        marker = "*" if branch == current else " "
        print(f"{marker} {branch}")
    return 0


def _ls_files(repo: Repository) -> int:
    for entry in repo.index_entries():
        print(entry.path)
    return 0


def _log(repo: Repository, args: list[str]) -> int:
    if args:
        GitCache(repo).rebuild(force=True)
        return _passthrough(["log", *args])
    oid = repo.ref(repo.current_branch)
    while oid is not None:
        commit = repo.load_commit(oid)
        print(f"commit {commit.id}")
        print(f"Author: {commit.author.name} <{commit.author.email}>")
        print()
        print(f"    {commit.message}")
        print()
        oid = commit.parents[0] if commit.parents else None
    return 0


def _rm(repo: Repository, args: list[str]) -> int:
    targets = [arg for arg in args if not arg.startswith("-")]
    if not targets:
        sys.stderr.write("git rm: missing pathspec\n")
        return 1
    for target in targets:
        rel = _relative(repo, target)
        path = repo.root / rel
        if path.exists() and path.is_file():
            path.unlink()
        repo.delete_file(rel)
    GitCache(repo).rebuild(force=True)
    return 0


def _mv(repo: Repository, args: list[str]) -> int:
    if len(args) != 2:
        sys.stderr.write("usage: git mv <source> <destination>\n")
        return 1
    source = _relative(repo, args[0])
    dest = _relative(repo, args[1])
    source_path = repo.root / source
    dest_path = repo.root / dest
    if source_path.exists():
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.rename(dest_path)
    data = repo.read_file(source)
    repo.delete_file(source)
    repo.write_file(dest, data)
    GitCache(repo).rebuild(force=True)
    return 0


def _restore(repo: Repository, args: list[str]) -> int:
    targets = [arg for arg in args if not arg.startswith("-")] or [entry.path for entry in repo.index_entries()]
    indexed = {entry.path: entry for entry in repo.index_entries()}
    for target in targets:
        rel = _relative(repo, target)
        entry = indexed.get(rel)
        path = repo.root / rel
        if entry is None:
            if path.exists() and path.is_file():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(repo.blob_payload(entry.oid))
    return 0


def _diff(repo: Repository, args: list[str]) -> int:
    indexed = {entry.path: entry for entry in repo.index_entries()}
    targets = [_relative(repo, arg) for arg in args if not arg.startswith("-")]
    paths = targets or sorted(indexed)
    for rel in paths:
        entry = indexed.get(rel)
        old = repo.blob_payload(entry.oid) if entry else b""
        path = repo.root / rel
        new = path.read_bytes() if path.exists() and path.is_file() else b""
        if old == new:
            continue
        old_lines = old.decode("utf-8", "replace").splitlines(keepends=True)
        new_lines = new.decode("utf-8", "replace").splitlines(keepends=True)
        sys.stdout.writelines(
            difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}")
        )
    return 0


def _show(repo: Repository, args: list[str]) -> int:
    target = args[0] if args else "HEAD"
    oid = _resolve(repo, target)
    if oid is None:
        sys.stderr.write(f"fatal: ambiguous argument '{target}'\n")
        return 128
    try:
        commit = repo.load_commit(oid)
    except Exception:
        sys.stdout.buffer.write(repo.object_data(oid))
        return 0
    print(f"commit {commit.id}")
    print(f"Author: {commit.author.name} <{commit.author.email}>")
    print()
    print(f"    {commit.message}")
    return 0


def _tag(repo: Repository, args: list[str]) -> int:
    if not args:
        for name, _ in repo.list_refs():
            if name.startswith("refs/tags/"):
                print(name.removeprefix("refs/tags/"))
        return 0
    head = repo.ref(repo.current_branch)
    if head is None:
        sys.stderr.write("fatal: cannot tag before first commit\n")
        return 1
    repo.set_ref(f"refs/tags/{args[0]}", head)
    GitCache(repo).rebuild(force=True)
    return 0


def _remote(repo: Repository, args: list[str]) -> int:
    if args in ([], ["-v"]):
        return _print_remote(repo, verbose=args == ["-v"])
    if args[:2] == ["add", "origin"] and len(args) >= 3:
        repo.set_meta("git_origin", args[2])
        GitCache(repo).rebuild(force=True)
        return 0
    if tuple(args[:2]) in {("remove", "origin"), ("rm", "origin")}:
        GitCache(repo).rebuild(force=True)
        code = _passthrough(["remote", *args])
        if code == 0:
            repo.delete_meta("git_origin")
            GitCache(repo).rebuild(force=True)
        return code
    GitCache(repo).rebuild(force=True)
    return _passthrough(["remote", *args])


def _print_remote(repo: Repository, *, verbose: bool) -> int:
    origin = repo.get_meta("git_origin") or repo.backend_url()
    if not origin:
        return 0
    if verbose:
        print(f"origin\t{origin} (fetch)")
        print(f"origin\t{origin} (push)")
    else:
        print("origin")
    return 0


def _push(repo: Repository, args: list[str]) -> int:
    mode = push_mode_from_env(repo.get_meta("push_mode", "mirror") or "mirror")
    if mode == "manual":
        return _passthrough(["push", *args])
    if mode == "mirror":
        git_code = 0
        if repo.get_meta("git_origin"):
            GitCache(repo).rebuild(force=True)
            git_code = _passthrough(["push", *args])
        trunks_code = _trunks_command("push")
        policy = mirror_policy_from_env(repo.get_meta("mirror_policy", "strict") or "strict")
        return (git_code or trunks_code) if policy == "strict" else trunks_code
    return _trunks_command("push")


def _working_tree_git(repo: Repository, argv: list[str]) -> int:
    GitCache(repo).rebuild(force=True)
    result = subprocess.run([system_git(), *argv], cwd=repo.root, env=_system_git_env())
    if result.returncode == 0:
        _import_git_state(repo)
        GitCache(repo).rebuild(force=True)
    return result.returncode


def _trunks_command(command: str) -> int:
    result = subprocess.run(
        [sys.executable, "-m", "trunks.cli", command],
        text=True,
        env=_subprocess_env(),
    )
    return result.returncode


def _subprocess_env() -> dict[str, str]:
    # Tests change cwd into a temp directory before invoking the gitshim, which
    # means the dev source tree is no longer on sys.path. Prepend the directory
    # containing the `trunks` package so the subprocess can import itself
    # whether or not the wheel is pip-installed.
    env = os.environ.copy()
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_parent}{os.pathsep}{existing}" if existing else package_parent
    return env


def _passthrough(argv: list[str]) -> int:
    result = subprocess.run([system_git(), *argv], env=_system_git_env())
    return result.returncode


def _system_git_env() -> dict[str, str]:
    return {**os.environ, "TRUNKS_BYPASS": "1"}


def _import_git_state(repo: Repository) -> None:
    git_dir = repo.root / ".git"
    objects_dir = git_dir / "objects"
    if objects_dir.exists():
        for directory in objects_dir.iterdir():
            if len(directory.name) != 2 or not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                try:
                    data = zlib.decompress(path.read_bytes())
                    repo.put_object(ObjectId(directory.name + path.name), data)
                except Exception:
                    continue
    head = _read_head(repo)
    if head is not None:
        repo.set_ref(repo.current_branch, head)
        repo.reset_index_to_commit(head)


def _read_head(repo: Repository) -> ObjectId | None:
    result = subprocess.run(
        [system_git(), "rev-parse", "HEAD"],
        cwd=repo.root,
        text=True,
        capture_output=True,
        env=_system_git_env(),
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    return ObjectId(raw) if raw else None


def _resolve(repo: Repository, value: str) -> ObjectId | None:
    if value == "HEAD":
        return repo.ref(repo.current_branch)
    try:
        return ObjectId(value)
    except ValueError:
        return repo.ref(value)


def _relative(repo: Repository, value: str) -> str:
    return _worktree_path(repo, value).relative_to(repo.root).as_posix()


def _worktree_path(repo: Repository, value: str) -> Path:
    path = Path(value)
    candidate = path if path.is_absolute() else repo.root / path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(repo.root)
    except ValueError as exc:
        raise InvalidPath(f"path outside repository: {value}") from exc
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
