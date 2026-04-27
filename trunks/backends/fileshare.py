from __future__ import annotations

from pathlib import Path

from .local import Local
from ..backend import Role


class FileShare(Local):
    def __init__(self, root: str | Path, *, role: Role = Role.primary) -> None:
        super().__init__(root, role=role)

