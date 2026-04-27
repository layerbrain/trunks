from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

@dataclass(frozen=True)
class Result:
    code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class ChangedFile:
    path: str
    data: bytes | None


class Session(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def checkout(self, files: list[ChangedFile]) -> None: ...

    @abstractmethod
    async def shell(self) -> int: ...

    @abstractmethod
    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        stdin: bytes | None,
    ) -> Result: ...

    @abstractmethod
    async def save(self) -> AsyncIterator[ChangedFile]: ...

    @abstractmethod
    async def destroy(self) -> None: ...


def changed_files(root: Path) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".trunks" not in path.parts and ".git" not in path.parts:
            files.append(ChangedFile(path.relative_to(root).as_posix(), path.read_bytes()))
    return files
