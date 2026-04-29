from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pydantic_ai import Agent


REPO = os.environ.get("TRUNKS_REPO", "my-app")
BACKEND = os.environ.get("TRUNKS_BACKEND")
WORKDIR = Path(os.environ.get("TRUNKS_PATH", f"./{REPO}")).resolve()
_MOUNTED = False

agent = Agent(
    os.environ.get("OPENAI_MODEL", "openai:gpt-4o-mini"),
    system_prompt=(
        "You work in a Trunks-backed repository. Pull first, inspect files, make focused changes, "
        "run relevant checks, checkpoint with a clear message, and push only after checks pass."
    ),
)


def _run_trunks(*args: str) -> None:
    subprocess.run(["trunks", *args], cwd=WORKDIR if WORKDIR.exists() else None, check=True)


def _ensure_workspace() -> None:
    global _MOUNTED
    if _MOUNTED:
        return
    command = ["trunks", "mount", "--repo", REPO, "--path", str(WORKDIR)]
    if BACKEND:
        command.extend(["--backend", BACKEND])
    subprocess.run(command, check=True)
    _MOUNTED = True


def _safe_path(path: str) -> Path:
    _ensure_workspace()
    target = (WORKDIR / path).resolve()
    if target != WORKDIR and WORKDIR not in target.parents:
        raise ValueError(f"path escapes workspace: {path}")
    return target


@agent.tool_plain
def pull() -> str:
    """Pull the latest repository state from Trunks storage."""
    _ensure_workspace()
    _run_trunks("pull")
    return "ok"


@agent.tool_plain
def list_files(path: str = ".") -> list[str]:
    """List files in the mounted Trunks repository."""
    return sorted(item.name for item in _safe_path(path).iterdir())


@agent.tool_plain
def read(path: str) -> str:
    """Read a UTF-8 file from the mounted Trunks repository."""
    return _safe_path(path).read_text(encoding="utf-8")


@agent.tool_plain
def write(path: str, content: str) -> str:
    """Write a UTF-8 file into the mounted Trunks repository."""
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return "ok"


@agent.tool_plain
def shell(command: str) -> dict[str, str | int]:
    """Run a shell command in the local mounted repository directory."""
    _ensure_workspace()
    result = subprocess.run(command, cwd=WORKDIR, shell=True, capture_output=True, text=True, check=False)
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}


@agent.tool_plain
def diff(vs: str = "main") -> str:
    """Show changed files against a branch, tag, or commit."""
    _ensure_workspace()
    result = subprocess.run(["trunks", "diff", "--vs", vs], cwd=WORKDIR, capture_output=True, text=True, check=False)
    return result.stdout or result.stderr


@agent.tool_plain
def checkpoint(message: str) -> str:
    """Create a Trunks checkpoint from current repository changes."""
    _ensure_workspace()
    _run_trunks("checkpoint", "-m", message)
    return "ok"


@agent.tool_plain
def push() -> str:
    """Push committed repository state to Trunks storage."""
    _ensure_workspace()
    _run_trunks("push")
    return "ok"


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or "Pull the repo, inspect the task, make the smallest safe change, test it, checkpoint, and push."
    print(agent.run_sync(prompt).output)
