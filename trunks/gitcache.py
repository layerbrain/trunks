from __future__ import annotations

import os
import shutil
import subprocess
import zlib
from pathlib import Path

from .errors import GitNotFound
from .repository import Repository


def _shim_marker_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "trunks" / "git-shim-installed"


def shim_marker_present() -> bool:
    return _shim_marker_path().exists()


GIT_NOT_FOUND_MESSAGE = """Git was not found.

Trunks storage is configured and the Python API still works, but the managed Git shell needs Git installed.

Install Git:
  macOS:   xcode-select --install
  Ubuntu:  sudo apt install git"""


class GitCache:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.root = repository.root / ".git"

    def _git_interop_active(self) -> bool:
        # Maintain .git/ only when the user opted into git interop:
        #   1. .git/ already exists (came from a real git repo, or was seeded
        #      by a previous Trunks run where the shim was installed), OR
        #   2. the user installed the trunks git shim, which writes a marker
        #      under XDG_CONFIG_HOME / .config/trunks/git-shim-installed.
        # Otherwise short-circuit so a fresh `trunks mount` does not surprise
        # the user with an unexplained .git/ directory next to .trunks/.
        return self.root.exists() or shim_marker_present()

    def rebuild(self, *, force: bool = False) -> None:
        # `force=True` is the signal that the caller IS the git-interop entry
        # point (gitshim being invoked through the user's PATH). In that case
        # we always materialise `.git/` because the very fact that gitshim
        # is running proves the user wants git interop right now.
        if not force and not self._git_interop_active():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (self.root / "refs" / "tags").mkdir(parents=True, exist_ok=True)
        (self.root / "info").mkdir(parents=True, exist_ok=True)
        (self.root / "HEAD").write_text(f"ref: refs/heads/{self.repository.current_branch}\n", encoding="utf-8")
        config = (
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tfilemode = true\n"
            "\tbare = false\n"
            "\tlogallrefupdates = false\n"
        )
        origin = self.repository.get_meta("git_origin") or self.repository.backend_url()
        if origin:
            config += f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        (self.root / "config").write_text(config, encoding="utf-8")
        (self.root / "info" / "exclude").write_text("", encoding="utf-8")
        for oid, data in self.repository.all_objects():
            directory = self.root / "objects" / oid.value[:2]
            path = directory / oid.value[2:]
            if path.exists():
                continue
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zlib.compress(data))
        for name, oid in self.repository.list_refs():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{oid}\n", encoding="utf-8")
        remote_heads = self.root / "refs" / "remotes" / "origin"
        shutil.rmtree(remote_heads, ignore_errors=True)
        for name, oid in self.repository.list_refs():
            if not name.startswith("refs/heads/"):
                continue
            path = remote_heads / name.removeprefix("refs/heads/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{oid}\n", encoding="utf-8")
        git = find_system_git()
        if git is not None and self.repository.ref(self.repository.current_branch) is not None:
            subprocess.run(
                [git, "read-tree", "--reset", "HEAD"],
                cwd=self.repository.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def system_git() -> str:
    git = find_system_git()
    if git is None:
        raise GitNotFound(GIT_NOT_FOUND_MESSAGE)
    return git


def find_system_git() -> str | None:
    import os
    import shutil
    import sys

    shim_dir = os.environ.get("TRUNKS_GIT_SHIM_DIR")
    current_executable = Path(sys.argv[0]).resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if shim_dir and Path(entry).resolve() == Path(shim_dir).resolve():
            continue
        candidate = Path(entry) / "git"
        if candidate.exists() and candidate.resolve() != current_executable:
            return str(candidate)
    return shutil.which("git")
