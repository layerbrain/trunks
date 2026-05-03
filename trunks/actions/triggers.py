from __future__ import annotations

from pathlib import Path

from trunks.repository import Repository

from .workflow import load_workflows, run_workflow


async def run_push_workflows(repo: Repository, *, commit: str) -> list[dict[str, object]]:
    root = Path(repo.root)
    workflows = load_workflows(root)
    runs: list[dict[str, object]] = []
    for workflow in workflows:
        if "push" not in workflow.triggers:
            continue
        runs.append(await run_workflow(repo, workflow=workflow.path, commit=commit, cwd=str(root)))
    return runs
