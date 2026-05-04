from __future__ import annotations

from pathlib import Path

from trunks.repository import Repository

from .workflow import (
    load_workflows,
    pull_request_trigger_matches,
    push_trigger_matches,
    run_workflow,
)


def _changed_files(repo: Repository, commit_oid: str) -> list[str]:
    try:
        commit = repo.load_commit(commit_oid)
    except Exception:
        return []
    current = {e.path: e.oid for e in repo.flatten_tree(commit.tree)}
    if not commit.parents:
        return sorted(current)
    try:
        parent = repo.load_commit(commit.parents[0])
    except Exception:
        return sorted(current)
    parent_map = {e.path: e.oid for e in repo.flatten_tree(parent.tree)}
    changed: set[str] = set()
    for path, oid in current.items():
        if path not in parent_map or parent_map[path] != oid:
            changed.add(path)
    for path in parent_map:
        if path not in current:
            changed.add(path)
    return sorted(changed)


async def run_push_workflows(
    repo: Repository,
    *,
    commit: str,
    branch: str | None = None,
    tag: str | None = None,
) -> list[dict[str, object]]:
    root = Path(repo.root)
    workflows = load_workflows(root)
    if branch is None:
        branch = repo.current_branch
    changed_files = _changed_files(repo, commit)
    runs: list[dict[str, object]] = []
    for workflow in workflows:
        filt = workflow.triggers.get("push")
        if filt is None:
            continue
        if not push_trigger_matches(filt, branch=branch, changed_files=changed_files, tag=tag):
            continue
        runs.append(await run_workflow(repo, workflow=workflow.path, commit=commit, cwd=str(root)))
    return runs


async def run_pull_request_workflows(
    repo: Repository,
    *,
    commit: str,
    branch: str,
    changed_files: list[str] | None = None,
    event_type: str = "synchronize",
) -> list[dict[str, object]]:
    root = Path(repo.root)
    workflows = load_workflows(root)
    if changed_files is None:
        changed_files = _changed_files(repo, commit)
    runs: list[dict[str, object]] = []
    for workflow in workflows:
        filt = workflow.triggers.get("pull_request")
        if filt is None:
            continue
        if not pull_request_trigger_matches(filt, branch=branch, changed_files=changed_files, event_type=event_type):
            continue
        runs.append(await run_workflow(repo, workflow=workflow.path, commit=commit, cwd=str(root)))
    return runs
