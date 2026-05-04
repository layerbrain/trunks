from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Awaitable

from trunks._auto import run_auto


def github_checks_writeback(
    *,
    token: str,
    owner: str,
    repo: str,
    commit: str,
    run: str,
    name: str,
    conclusion: str,
    details_url: str | None = None,
    api_base: str = "https://api.github.com",
) -> dict[str, object] | Awaitable[dict[str, object]]:
    return run_auto(
        lambda: github_checks_writeback_async(
            token=token,
            owner=owner,
            repo=repo,
            commit=commit,
            run=run,
            name=name,
            conclusion=conclusion,
            details_url=details_url,
            api_base=api_base,
        )
    )


async def github_checks_writeback_async(
    *,
    token: str,
    owner: str,
    repo: str,
    commit: str,
    run: str,
    name: str,
    conclusion: str,
    details_url: str | None = None,
    api_base: str = "https://api.github.com",
) -> dict[str, object]:
    import asyncio

    return await asyncio.to_thread(
        _post_check_run,
        token=token,
        owner=owner,
        repo=repo,
        commit=commit,
        run=run,
        name=name,
        conclusion=conclusion,
        details_url=details_url,
        api_base=api_base,
    )


def _post_check_run(
    *,
    token: str,
    owner: str,
    repo: str,
    commit: str,
    run: str,
    name: str,
    conclusion: str,
    details_url: str | None,
    api_base: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "head_sha": commit,
        "status": "completed",
        "conclusion": _github_conclusion(conclusion),
        "external_id": run,
    }
    if details_url:
        payload["details_url"] = details_url
    body = json.dumps(payload, sort_keys=True).encode()
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/repos/{owner}/{repo}/check-runs",
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
    return json.loads(raw.decode()) if raw else {"ok": True}


def _github_conclusion(phase: str) -> str:
    if phase == "succeeded":
        return "success"
    if phase == "canceled":
        return "cancelled"
    if phase == "skipped":
        return "skipped"
    return "failure"


def github_checks_writeback_from_env(run: dict[str, object]) -> dict[str, object] | None:
    request = _github_writeback_request_from_env(run)
    if request is None:
        return None
    result = github_checks_writeback(**request)
    if hasattr(result, "__await__"):
        raise RuntimeError("github_checks_writeback_from_env must be called outside a running event loop")
    return result  # type: ignore[return-value]


async def github_checks_writeback_from_env_async(run: dict[str, object]) -> dict[str, object] | None:
    request = _github_writeback_request_from_env(run)
    if request is None:
        return None
    return await github_checks_writeback_async(**request)


def _github_writeback_request_from_env(run: dict[str, object]) -> dict[str, str | None]:
    token = os.environ.get("TRUNKS_GITHUB_CHECKS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("TRUNKS_GITHUB_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository or "/" not in repository:
        return None
    commit = run.get("commit")
    run_id = run.get("id")
    state = run.get("state")
    if not isinstance(commit, str) or not isinstance(run_id, str) or not isinstance(state, dict):
        return None
    owner, repo = repository.split("/", 1)
    details_url = os.environ.get("TRUNKS_ACTIONS_DETAILS_URL")
    if details_url:
        details_url = details_url.format(run=run_id, commit=commit)
    return {
        "token": token,
        "owner": owner,
        "repo": repo,
        "commit": commit,
        "run": run_id,
        "name": os.environ.get("TRUNKS_GITHUB_CHECK_NAME", "Trunks Actions"),
        "conclusion": str(state.get("phase") or "failed"),
        "details_url": details_url,
        "api_base": os.environ.get("TRUNKS_GITHUB_API_BASE", "https://api.github.com"),
    }
