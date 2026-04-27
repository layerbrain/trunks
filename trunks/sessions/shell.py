from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import AsyncIterator

from ..session import ChangedFile, Result, Session


class Shell(Session):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._checked_out: dict[str, bytes] = {}

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def checkout(self, files: list[ChangedFile]) -> None:
        self._checked_out = {}
        for item in files:
            path = self.root / item.path
            if item.data is None:
                if path.exists() and path.is_file():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.data)
            self._checked_out[item.path] = item.data

    async def shell(self) -> int:
        shell = os.environ.get("SHELL") or shutil.which("sh") or "/bin/sh"
        return await asyncio.to_thread(subprocess.call, [shell], cwd=self.root)

    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        stdin: bytes | None,
    ) -> Result:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.root / cwd,
            env={**os.environ, **env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(stdin)
        return Result(proc.returncode or 0, stdout, stderr)

    async def save(self) -> AsyncIterator[ChangedFile]:
        current: dict[str, bytes] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and ".trunks" not in path.parts and ".git" not in path.parts:
                current[path.relative_to(self.root).as_posix()] = path.read_bytes()
        for rel in sorted(set(self._checked_out) | set(current)):
            before = self._checked_out.get(rel)
            after = current.get(rel)
            if before == after:
                continue
            yield ChangedFile(rel, after)

    async def destroy(self) -> None:
        return None
