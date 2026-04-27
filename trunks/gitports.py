from __future__ import annotations

import subprocess
from pathlib import Path

from .repository import Repository


class GitImport:
    def __init__(self, repository: Repository, root: Path | None = None) -> None:
        self.repository = repository
        self.root = root or repository.root

    def import_committed_state(self) -> None:
        if not (self.root / ".git").exists():
            return
        files = subprocess.run(
            ["git", "ls-files"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if files.returncode != 0:
            return
        for raw in files.stdout.splitlines():
            path = self.root / raw
            if path.exists() and path.is_file():
                self.repository.write_file(raw, path.read_bytes())


class GitExport:
    def __init__(self, repository: Repository, root: Path | None = None) -> None:
        self.repository = repository
        self.root = root or repository.root

    def export_branch(self, branch: str | None = None) -> None:
        oid = self.repository.ref(branch or self.repository.current_branch)
        if oid is not None:
            self.repository.checkout_tree(oid)

