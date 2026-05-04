from __future__ import annotations

import os
import shutil
import subprocess
import zlib
from pathlib import Path

from .errors import GitNotFound
from .repository import Repository


MANAGED_GIT_MARKER = "trunks-managed"


def _shim_marker_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "trunks" / "git-shim-installed"


def is_trunks_managed_git_dir(git_dir: Path) -> bool:
    return (git_dir / MANAGED_GIT_MARKER).is_file()


def is_foreign_git_dir(git_dir: Path) -> bool:
    return git_dir.exists() and not is_trunks_managed_git_dir(git_dir)


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
        return is_trunks_managed_git_dir(self.root)

    def rebuild(self, *, force: bool = False) -> None:
        if is_foreign_git_dir(self.root):
            return
        if not force and not self._git_interop_active():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / MANAGED_GIT_MARKER).write_text("1\n", encoding="utf-8")
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        refs = self.root / "refs"
        shutil.rmtree(refs / "heads", ignore_errors=True)
        shutil.rmtree(refs / "tags", ignore_errors=True)
        shutil.rmtree(refs / "actions", ignore_errors=True)
        (refs / "heads").mkdir(parents=True, exist_ok=True)
        (refs / "tags").mkdir(parents=True, exist_ok=True)
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
        if not origin:
            origin = _read_existing_origin(self.root / "config")
        if origin:
            config += f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        existing_extra = _read_extra_config_sections(self.root / "config")
        if existing_extra:
            config += existing_extra
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
            if not _is_git_visible_ref(name):
                continue
            path = self.root / name
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


def _read_existing_origin(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    in_remote = False
    for line in config_path.read_text(encoding="utf-8").splitlines():
        if line.strip() == '[remote "origin"]':
            in_remote = True
            continue
        if in_remote and line.strip().startswith("url = "):
            return line.strip().removeprefix("url = ")
        if line.strip().startswith("[") and in_remote:
            break
    return None


def _is_git_visible_ref(name: str) -> bool:
    return name.startswith("refs/heads/") or name.startswith("refs/tags/")


def _read_extra_config_sections(config_path: Path) -> str:
    if not config_path.exists():
        return ""
    lines = config_path.read_text(encoding="utf-8").splitlines()
    extra: list[str] = []
    skip = True
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            skip = stripped == "[core]" or stripped == '[remote "origin"]'
        if not skip:
            extra.append(line)
    if not extra:
        return ""
    return "\n".join(extra) + "\n"


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
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    for entry in path_entries:
        if shim_dir and Path(entry).resolve() == Path(shim_dir).resolve():
            continue
        candidate = Path(entry) / "git"
        if candidate.exists() and candidate.resolve() != current_executable and not _is_trunks_git_shim(candidate):
            return str(candidate)
    if not path_entries:
        return None
    for value in (shutil.which("git"), "/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if not value:
            continue
        candidate = Path(value)
        if candidate.exists() and candidate.resolve() != current_executable and not _is_trunks_git_shim(candidate):
            return str(candidate)
    return None


def _is_trunks_git_shim(path: Path) -> bool:
    try:
        data = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"trunks.gitshim" in data or b"TRUNKS_GIT_SHIM_DIR" in data
