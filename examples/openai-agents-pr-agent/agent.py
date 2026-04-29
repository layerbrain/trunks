from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agents import Agent, Runner, function_tool


REPO = os.environ.get("TRUNKS_REPO", "my-app")
BACKEND = os.environ.get("TRUNKS_BACKEND")
WORKDIR = Path(os.environ.get("TRUNKS_PATH", f"./{REPO}")).resolve()
_MOUNTED = False


def _run_trunks(*args: str) -> None:
    command = ["trunks", *args]
    subprocess.run(command, cwd=WORKDIR if WORKDIR.exists() else None, check=True)


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


@function_tool
def pull() -> str:
    """Pull the latest repository state from Trunks storage."""
    _ensure_workspace()
    _run_trunks("pull")
    return "ok"


@function_tool
def list_files(path: str = ".") -> list[str]:
    """List files in the mounted Trunks repository."""
    target = _safe_path(path)
    return sorted(item.name for item in target.iterdir())


@function_tool
def read(path: str) -> str:
    """Read a UTF-8 file from the mounted Trunks repository."""
    return _safe_path(path).read_text(encoding="utf-8")


@function_tool
def write(path: str, content: str) -> str:
    """Write a UTF-8 file into the mounted Trunks repository."""
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return "ok"


@function_tool
def shell(command: str) -> dict[str, str | int]:
    """Run a shell command in the local mounted repository directory."""
    _ensure_workspace()
    result = subprocess.run(command, cwd=WORKDIR, shell=True, capture_output=True, text=True, check=False)
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}


@function_tool
def diff(vs: str = "main") -> str:
    """Show changed files against a branch, tag, or commit."""
    _ensure_workspace()
    result = subprocess.run(["trunks", "diff", "--vs", vs], cwd=WORKDIR, capture_output=True, text=True, check=False)
    return result.stdout or result.stderr


@function_tool
def checkpoint(message: str) -> str:
    """Create a Trunks checkpoint from current repository changes."""
    _ensure_workspace()
    _run_trunks("checkpoint", "-m", message)
    return "ok"


@function_tool
def push() -> str:
    """Push committed repository state to Trunks storage."""
    _ensure_workspace()
    _run_trunks("push")
    return "ok"


agent = Agent(
    name="trunks-pr-agent",
    instructions=(
        "You work in a Trunks-backed repository. Pull first, inspect files, make focused changes, "
        "run relevant checks, checkpoint with a clear message, and push only after checks pass."
    ),
    tools=[pull, list_files, read, write, shell, diff, checkpoint, push],
)


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or "Pull the repo, inspect the task, make the smallest safe change, test it, checkpoint, and push."
    print(Runner.run_sync(agent, prompt).final_output)
