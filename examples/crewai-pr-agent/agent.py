from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Type

from crewai import Agent, Crew, Task
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


REPO = os.environ.get("TRUNKS_REPO", "my-app")
BACKEND = os.environ.get("TRUNKS_BACKEND")
WORKDIR = Path(os.environ.get("TRUNKS_PATH", f"./{REPO}")).resolve()
_MOUNTED = False


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


class PathInput(BaseModel):
    path: str = Field(".", description="Repository path")


class WriteInput(BaseModel):
    path: str
    content: str


class CommandInput(BaseModel):
    command: str


class MessageInput(BaseModel):
    message: str


class DiffInput(BaseModel):
    vs: str = Field("main", description="Branch, tag, or commit to diff against")


class PullTool(BaseTool):
    name: str = "trunks_pull"
    description: str = "Pull the latest repository state from Trunks storage."

    def _run(self) -> str:
        _ensure_workspace()
        _run_trunks("pull")
        return "ok"


class ListTool(BaseTool):
    name: str = "trunks_list"
    description: str = "List files in the mounted Trunks repository."
    args_schema: Type[BaseModel] = PathInput

    def _run(self, path: str = ".") -> list[str]:
        return sorted(item.name for item in _safe_path(path).iterdir())


class ReadTool(BaseTool):
    name: str = "trunks_read"
    description: str = "Read a UTF-8 file from the mounted Trunks repository."
    args_schema: Type[BaseModel] = PathInput

    def _run(self, path: str = ".") -> str:
        return _safe_path(path).read_text(encoding="utf-8")


class WriteTool(BaseTool):
    name: str = "trunks_write"
    description: str = "Write a UTF-8 file into the mounted Trunks repository."
    args_schema: Type[BaseModel] = WriteInput

    def _run(self, path: str, content: str) -> str:
        target = _safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return "ok"


class ShellTool(BaseTool):
    name: str = "trunks_shell"
    description: str = "Run a shell command in the local mounted repository directory."
    args_schema: Type[BaseModel] = CommandInput

    def _run(self, command: str) -> str:
        _ensure_workspace()
        result = subprocess.run(command, cwd=WORKDIR, shell=True, capture_output=True, text=True, check=False)
        return f"exit_code={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


class DiffTool(BaseTool):
    name: str = "trunks_diff"
    description: str = "Show changed files against a branch, tag, or commit."
    args_schema: Type[BaseModel] = DiffInput

    def _run(self, vs: str = "main") -> str:
        _ensure_workspace()
        result = subprocess.run(["trunks", "diff", "--vs", vs], cwd=WORKDIR, capture_output=True, text=True, check=False)
        return result.stdout or result.stderr


class CheckpointTool(BaseTool):
    name: str = "trunks_checkpoint"
    description: str = "Create a Trunks checkpoint from current repository changes."
    args_schema: Type[BaseModel] = MessageInput

    def _run(self, message: str) -> str:
        _ensure_workspace()
        _run_trunks("checkpoint", "-m", message)
        return "ok"


class PushTool(BaseTool):
    name: str = "trunks_push"
    description: str = "Push committed repository state to Trunks storage."

    def _run(self) -> str:
        _ensure_workspace()
        _run_trunks("push")
        return "ok"


tools = [PullTool(), ListTool(), ReadTool(), WriteTool(), ShellTool(), DiffTool(), CheckpointTool(), PushTool()]

agent = Agent(
    role="Trunks PR agent",
    goal="Make focused repository changes and push checkpoints to Trunks storage.",
    backstory="You operate in a Trunks-backed repository and push only after checks pass.",
    tools=tools,
)

prompt = " ".join(sys.argv[1:]) or "Pull the repo, inspect the task, make the smallest safe change, test it, checkpoint, and push."

task = Task(
    description=prompt,
    expected_output="Summary of the pushed change.",
    agent=agent,
)

if __name__ == "__main__":
    print(Crew(agents=[agent], tasks=[task]).kickoff())
