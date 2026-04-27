from __future__ import annotations

import subprocess

from .gitcache import system_git
from .ids import ObjectId
from .objects import canonical_object
from .refs import branch_name, normalize_ref
from .repository import Repository


def import_existing_git_repository(repo: Repository) -> None:
    if not (repo.root / ".git").exists():
        return
    git = system_git()
    if not _git_ok(git, repo, "rev-parse", "--is-inside-work-tree"):
        return

    origin = _git_output(git, repo, "config", "--get", "remote.origin.url")
    if origin:
        repo.set_meta("git_origin", origin)

    current = _git_output(git, repo, "branch", "--show-current")
    if current:
        repo.set_current_branch(current)

    _import_objects(git, repo)
    _import_refs(git, repo)

    head = repo.ref(repo.current_branch)
    if head is not None:
        repo.reset_index_to_commit(head)


def _git_ok(git: str, repo: Repository, *args: str) -> bool:
    return subprocess.run(
        [git, *args],
        cwd=repo.root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _git_output(git: str, repo: Repository, *args: str) -> str | None:
    result = subprocess.run(
        [git, *args],
        cwd=repo.root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _import_objects(git: str, repo: Repository) -> None:
    with subprocess.Popen(
        [git, "cat-file", "--batch-all-objects", "--batch"],
        cwd=repo.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        assert process.stdout is not None
        while True:
            header = process.stdout.readline()
            if not header:
                break
            oid, kind, raw_size = header.decode("ascii").rstrip("\n").split(" ", 2)
            size = int(raw_size)
            payload = process.stdout.read(size)
            process.stdout.read(1)
            repo.put_object(ObjectId(oid), canonical_object(kind, payload))
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"git object import failed: {stderr.strip()}")


def _import_refs(git: str, repo: Repository) -> None:
    result = subprocess.run(
        [git, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"],
        cwd=repo.root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git ref import failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        ref, oid = line.split(" ", 1)
        repo.set_ref(ref, ObjectId(oid))

    head = _git_output(git, repo, "rev-parse", "HEAD")
    if head and repo.ref(repo.current_branch) is None:
        repo.set_ref(normalize_ref(repo.current_branch), ObjectId(head))
    if head:
        current_ref = _git_output(git, repo, "symbolic-ref", "-q", "HEAD")
        if current_ref:
            repo.set_current_branch(branch_name(current_ref))
