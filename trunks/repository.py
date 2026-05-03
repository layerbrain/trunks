from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import RepoConfig
from .errors import InvalidPath, ObjectNotFound, RefConflict, RepositoryCorrupt, RepositoryNotFound
from .ids import ObjectId, ulid
from .index import IndexEntry
from .journal import JournalEntry
from .objects import (
    Blob,
    Commit,
    CommitRecord,
    Identity,
    Tree,
    TreeEntry,
    canonical_object,
    object_id,
    parse_canonical_object,
    parse_commit_payload,
    parse_tree_payload,
)
from .paths import is_ignored, is_internal_path, normalize_path
from .refs import branch_name, normalize_ref
from .session import ChangedFile
from .sandboxes.profile import SandboxProviderProfile
from .storage import Storage


SCHEMA_VERSION = 1
DEFAULT_AUTHOR = Identity("Trunks", "trunks@local", datetime.now(UTC))


@dataclass(frozen=True)
class Status:
    branch: str
    head: ObjectId | None
    indexed: int
    dirty: bool
    backend: str | None
    untracked: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()


class Repository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.root = (self.path.parent.parent if self.path.parent.name == ".trunks" else Path.cwd()).resolve()

    @classmethod
    def default_path(cls, cwd: str | Path | None = None, name: str | None = None) -> Path:
        root = Path(cwd or Path.cwd())
        repo_name = name or root.name or "repo"
        return root / ".trunks" / f"{repo_name}.trunk"

    @classmethod
    def find(cls, cwd: str | Path | None = None) -> "Repository":
        root = Path(cwd or Path.cwd()).resolve()
        trunks_dir = root / ".trunks"
        if not trunks_dir.exists():
            raise RepositoryNotFound(f"no .trunks repository in {root}")
        matches = sorted(trunks_dir.glob("*.trunk"))
        if not matches:
            raise RepositoryNotFound(f"no .trunks/*.trunk repository in {root}")
        return cls(matches[0])

    @classmethod
    def init(
        cls,
        cwd: str | Path | None = None,
        *,
        name: str | None = None,
        backend: str | None = None,
        push_mode: str = "trunks-only",
    ) -> "Repository":
        path = cls.default_path(cwd, name)
        repo = cls(path)
        repo._initialize(RepoConfig(path.stem, backend, push_mode))
        return repo

    def _initialize(self, config: RepoConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                "insert or replace into meta(key, value) values (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.execute("insert or ignore into meta(key, value) values (?, ?)", ("current_branch", "main"))
            conn.execute("insert or replace into meta(key, value) values (?, ?)", ("repo_name", config.name))
            conn.execute("insert or replace into meta(key, value) values (?, ?)", ("push_mode", config.push_mode))
            if config.backend:
                conn.execute("insert or replace into meta(key, value) values (?, ?)", ("backend", config.backend))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        else:
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma journal_mode=wal")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            create table if not exists meta(key text primary key, value text not null);
            create table if not exists objects(
                oid text primary key,
                kind text not null,
                data blob not null
            );
            create table if not exists refs(name text primary key, oid text not null);
            create table if not exists backend_refs(name text primary key, oid text not null);
            create table if not exists index_entries(
                path text primary key,
                oid text not null,
                mode text not null
            );
            create table if not exists journals(id text primary key, kind text not null, data text not null);
            create table if not exists labels(commit_oid text not null, key text not null, value text not null);
            create table if not exists push_transactions(
                id text primary key,
                source_ref text not null,
                expected_remote_ref text,
                result_ref text not null,
                object_ids text not null,
                status text not null
            );
            create table if not exists storages(
                name text primary key,
                role text not null,
                backend text not null,
                data text not null
            );
            create table if not exists sandbox_providers(
                name text primary key,
                type text not null,
                priority integer not null default 100,
                enabled integer not null default 1,
                data text not null
            );
            """
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("select value from meta where key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("insert or replace into meta(key, value) values (?, ?)", (key, value))

    def delete_meta(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from meta where key = ?", (key,))

    @property
    def name(self) -> str:
        return self.get_meta("repo_name", self.path.stem) or self.path.stem

    @property
    def current_branch(self) -> str:
        return self.get_meta("current_branch", "main") or "main"

    def set_current_branch(self, branch: str) -> None:
        self.set_meta("current_branch", branch)

    def backend_url(self) -> str | None:
        return self.get_meta("backend")

    def set_backend_url(self, value: str) -> None:
        if value:
            self.set_primary_storage("primary", value)
        else:
            current_primary = self.primary_storage_name()
            self.delete_meta("backend")
            self.delete_meta("primary_storage_name")
            self.remove_storage_profile(current_primary)

    def primary_storage_name(self) -> str:
        return self.get_meta("primary_storage_name", "primary") or "primary"

    def set_primary_storage(self, name: str, url: str) -> None:
        self.set_meta("primary_storage_name", name)
        self.set_meta("backend", url)
        self.set_storage_profile(Storage.from_url(name=name, role="primary", url=url))

    def storage_targets(self) -> list[dict[str, object]]:
        profiles = self.list_storage_profiles()
        if profiles:
            return [profile.public_record(self.name) for profile in profiles]
        targets: list[dict[str, str]] = []
        if self.backend_url():
            targets.append({"name": self.primary_storage_name(), "role": "primary", "url": self.backend_url() or ""})
        targets.extend({"name": name, "role": "mirror", "url": url} for name, url in self.mirror_targets())
        return targets

    def storage_config(self) -> dict[str, object]:
        return {
            "format": 1,
            "repository": self.name,
            "storage": self.storage_targets(),
        }

    def apply_storage_config(self, data: dict[str, object]) -> None:
        storage = data.get("storage")
        if not isinstance(storage, list):
            return
        primary: tuple[str, str] | None = None
        mirrors: list[tuple[str, str]] = []
        for item in storage:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            role = item.get("role")
            url = item.get("url")
            if not isinstance(name, str) or not isinstance(role, str) or not isinstance(url, str):
                continue
            if role == "primary" and primary is None:
                primary = (name, url)
            elif role == "mirror":
                mirrors.append((name, url))
        if primary is not None:
            name, url = primary
            item = self._public_storage_item(storage, name, "primary")
            self._apply_public_storage_target(name, "primary", url, item)
        self.set_mirror_targets(mirrors)
        for name, url in mirrors:
            item = self._public_storage_item(storage, name, "mirror")
            self._apply_public_storage_target(name, "mirror", url, item)

    def _public_storage_item(self, storage: list[object], name: str, role: str) -> dict[str, object] | None:
        for item in storage:
            if not isinstance(item, dict):
                continue
            if item.get("name") == name and item.get("role") == role:
                return item
        return None

    def _apply_public_storage_target(
        self,
        name: str,
        role: str,
        url: str,
        item: dict[str, object] | None = None,
    ) -> None:
        existing = self.storage_profile(name)
        if existing is None:
            self.set_storage_profile(self._storage_from_public_target(name, role, url, item))
            return
        if existing.role != role:
            existing = Storage(
                name=existing.name,
                backend=existing.backend,
                role=role,
                settings=existing.settings,
                credentials=existing.credentials,
            )
            self.set_storage_profile(existing)
            return
        if role == "primary":
            self.set_meta("primary_storage_name", name)
            self.set_meta("backend", url)

    def _storage_from_public_target(
        self,
        name: str,
        role: str,
        url: str,
        item: dict[str, object] | None,
    ) -> Storage:
        backend = item.get("backend") if item else None
        settings = item.get("settings") if item else None
        if isinstance(backend, str) and isinstance(settings, dict):
            return Storage(
                name=name,
                backend=backend,
                role=role,
                settings={str(key): str(value) for key, value in settings.items() if value is not None},
            )
        return Storage.from_url(name=name, role=role, url=url)

    def storage_url(self, name: str) -> str | None:
        profile = self.storage_profile(name)
        if profile is not None:
            return profile.url(self.name)
        if name in {"primary", "remote", self.primary_storage_name()}:
            return self.backend_url()
        for mirror_name, url in self.mirror_targets():
            if mirror_name == name:
                return url
        return None

    def mirror_urls(self) -> list[str]:
        return [url for _, url in self.mirror_targets()]

    def mirror_targets(self) -> list[tuple[str, str]]:
        raw = self.get_meta("mirror_targets")
        if raw:
            try:
                values = json.loads(raw)
            except json.JSONDecodeError:
                return []
            return [
                (item["name"], item["url"])
                for item in values
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("url"), str)
                and item["name"]
                and item["url"]
            ]
        raw = self.get_meta("mirrors", "[]") or "[]"
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [
            (f"mirror-{idx + 1}", value)
            for idx, value in enumerate(values)
            if isinstance(value, str) and value
        ]

    def set_mirror_urls(self, values: list[str]) -> None:
        self.set_mirror_targets([(f"mirror-{idx + 1}", value) for idx, value in enumerate(values)])

    def set_mirror_targets(self, values: list[tuple[str, str]]) -> None:
        self.set_meta("mirror_targets", json.dumps([{"name": name, "url": url} for name, url in values]))
        self.set_meta("mirrors", json.dumps([url for _, url in values]))

    def set_mirror_storage(self, name: str, url: str) -> None:
        targets = [(current_name, current_url) for current_name, current_url in self.mirror_targets() if current_name != name]
        targets.append((name, url))
        self.set_mirror_targets(targets)
        self.set_storage_profile(Storage.from_url(name=name, role="mirror", url=url))

    def remove_storage(self, name: str) -> bool:
        if name in {"primary", "remote", self.primary_storage_name()} and self.backend_url():
            current_primary = self.primary_storage_name()
            self.set_backend_url("")
            self.remove_storage_profile(current_primary)
            return True
        targets = [(current_name, current_url) for current_name, current_url in self.mirror_targets() if current_name != name]
        if len(targets) == len(self.mirror_targets()):
            return False
        self.set_mirror_targets(targets)
        self.remove_storage_profile(name)
        return True

    def set_storage_profile(self, storage: Storage) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into storages(name, role, backend, data) values (?, ?, ?, ?) "
                "on conflict(name) do update set role = excluded.role, backend = excluded.backend, data = excluded.data",
                (storage.name, storage.role, storage.backend, json.dumps(storage.to_record(), sort_keys=True)),
            )
        if storage.role == "primary":
            self.set_meta("primary_storage_name", storage.name)
            self.set_meta("backend", storage.url(self.name))
        elif storage.role == "mirror":
            targets = [(name, url) for name, url in self.mirror_targets() if name != storage.name]
            targets.append((storage.name, storage.url(self.name)))
            self.set_mirror_targets(targets)

    def storage_profile(self, name: str) -> Storage | None:
        with self._connect() as conn:
            row = conn.execute("select data from storages where name = ?", (name,)).fetchone()
        if row is None:
            return None
        return Storage.from_record(json.loads(row["data"]))

    def list_storage_profiles(self) -> list[Storage]:
        with self._connect() as conn:
            rows = conn.execute("select data from storages order by role desc, name").fetchall()
        return [Storage.from_record(json.loads(row["data"])) for row in rows]

    def primary_storage_profile(self) -> Storage | None:
        return self.storage_profile(self.primary_storage_name())

    def mirror_storage_profiles(self) -> list[Storage]:
        profiles = self.list_storage_profiles()
        if profiles:
            return [profile for profile in profiles if profile.role == "mirror"]
        return [Storage.from_url(name=name, role="mirror", url=url) for name, url in self.mirror_targets()]

    def remove_storage_profile(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from storages where name = ?", (name,))

    def set_sandbox_provider_profile(self, profile: SandboxProviderProfile) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into sandbox_providers(name, type, priority, enabled, data) "
                "values (?, ?, ?, ?, ?) "
                "on conflict(name) do update set "
                "type = excluded.type, priority = excluded.priority, "
                "enabled = excluded.enabled, data = excluded.data",
                (
                    profile.name,
                    profile.type,
                    profile.priority,
                    1 if profile.enabled else 0,
                    json.dumps(profile.to_record(), sort_keys=True),
                ),
            )

    def sandbox_provider_profile(self, name: str) -> SandboxProviderProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                "select data from sandbox_providers where name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        return SandboxProviderProfile.from_record(json.loads(row["data"]))

    def list_sandbox_provider_profiles(self) -> list[SandboxProviderProfile]:
        with self._connect() as conn:
            rows = conn.execute(
                "select data from sandbox_providers order by priority asc, name asc"
            ).fetchall()
        return [SandboxProviderProfile.from_record(json.loads(row["data"])) for row in rows]

    def remove_sandbox_provider_profile(self, name: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("delete from sandbox_providers where name = ?", (name,))
            return cursor.rowcount > 0

    def write_file(self, path: str, data: bytes, *, branch: str | None = None) -> None:
        normalized = self._normalize_trackable_path(path)
        canonical = canonical_object("blob", data)
        oid = ObjectId.from_bytes(canonical)
        with self._connect() as conn:
            conn.execute(
                "insert or ignore into objects(oid, kind, data) values (?, ?, ?)",
                (str(oid), "blob", canonical),
            )
            conn.execute(
                "insert into index_entries(path, oid, mode) values (?, ?, ?) "
                "on conflict(path) do update set oid = excluded.oid, mode = excluded.mode",
                (normalized, str(oid), "100644"),
            )

    def stage_blob(self, data: bytes) -> ObjectId:
        canonical = canonical_object("blob", data)
        oid = ObjectId.from_bytes(canonical)
        with self._connect() as conn:
            conn.execute(
                "insert or ignore into objects(oid, kind, data) values (?, ?, ?)",
                (str(oid), "blob", canonical),
            )
        return oid

    def set_file_object(self, path: str, oid: ObjectId, *, mode: str = "100644") -> None:
        normalized = self._normalize_trackable_path(path)
        with self._connect() as conn:
            conn.execute(
                "insert into index_entries(path, oid, mode) values (?, ?, ?) "
                "on conflict(path) do update set oid = excluded.oid, mode = excluded.mode",
                (normalized, str(oid), mode),
            )

    def read_file(self, path: str, *, branch: str | None = None) -> bytes:
        normalized = self._normalize_trackable_path(path)
        with self._connect() as conn:
            row = conn.execute("select oid from index_entries where path = ?", (normalized,)).fetchone()
        if row:
            return self.blob_payload(ObjectId(row["oid"]))
        head = self.ref(branch or self.current_branch)
        if head is None:
            raise ObjectNotFound(normalized)
        tree = self.load_commit(head).tree
        oid = self.resolve_path(tree, normalized)
        if oid is None:
            raise ObjectNotFound(normalized)
        return self.blob_payload(oid)

    def delete_file(self, path: str, *, branch: str | None = None) -> None:
        normalized = self._normalize_trackable_path(path)
        with self._connect() as conn:
            conn.execute("delete from index_entries where path = ?", (normalized,))

    def copy_file(self, source: str, dest: str, *, branch: str | None = None) -> None:
        self.write_file(dest, self.read_file(source, branch=branch), branch=branch)

    def move_file(self, source: str, dest: str, *, branch: str | None = None) -> None:
        self.copy_file(source, dest, branch=branch)
        self.delete_file(source, branch=branch)

    def make_dir(self, path: str) -> None:
        if path not in {"", ".", "/"}:
            self._normalize_trackable_path(path)

    def list_dir(self, path: str = "", *, branch: str | None = None) -> list[str]:
        normalized = "" if path in {"", ".", "/"} else self._normalize_trackable_path(path)
        entries = self._entries_for_listing(branch=branch)
        prefix = f"{normalized}/" if normalized else ""
        children: set[str] = set()
        for entry in entries:
            if normalized and entry.path == normalized:
                continue
            if prefix and not entry.path.startswith(prefix):
                continue
            rest = entry.path[len(prefix) :] if prefix else entry.path
            if rest:
                children.add(rest.split("/", 1)[0])
        return sorted(children)

    def exists(self, path: str, *, branch: str | None = None) -> bool:
        normalized = "" if path in {"", ".", "/"} else self._normalize_trackable_path(path)
        if not normalized:
            return True
        entries = self._entries_for_listing(branch=branch)
        prefix = f"{normalized}/"
        return any(entry.path == normalized or entry.path.startswith(prefix) for entry in entries)

    def _entries_for_listing(self, *, branch: str | None = None) -> list[IndexEntry]:
        if branch is None or branch == self.current_branch:
            return self.index_entries()
        head = self.ref(branch)
        if head is None:
            raise ObjectNotFound(branch)
        return self.flatten_tree(self.load_commit(head).tree)

    def add_worktree_path(self, path: Path, *, force: bool = False) -> None:
        if path.is_symlink():
            return
        path = path.resolve(strict=False)
        self._relative_worktree_path(path)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if not child.is_file() or child.is_symlink():
                    continue
                rel = self._relative_worktree_path(child.resolve(strict=False))
                if is_internal_path(rel):
                    continue
                if force or not is_ignored(rel, self.root):
                    self.add_worktree_path(child, force=force)
            return
        rel = self._relative_worktree_path(path)
        normalized = normalize_path(rel)
        if is_internal_path(normalized):
            return
        if not force and is_ignored(normalized, self.root):
            return
        self.write_file(normalized, path.read_bytes())

    def add_all_worktree_files(self, *, force: bool = False) -> None:
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = self._relative_worktree_path(path.resolve(strict=False))
            if is_internal_path(rel):
                continue
            if force or not is_ignored(rel, self.root):
                self.add_worktree_path(path, force=force)

    def _relative_worktree_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise InvalidPath(f"path outside repository: {path}") from exc

    def _normalize_trackable_path(self, path: str) -> str:
        normalized = normalize_path(path)
        if is_internal_path(normalized):
            raise InvalidPath(f"internal Trunks/Git path is not trackable: {path}")
        return normalized

    def _safe_worktree_file_path(self, path: str) -> Path:
        normalized = self._normalize_trackable_path(path)
        candidate = (self.root / normalized).resolve(strict=False)
        self._relative_worktree_path(candidate)
        return candidate

    def _validate_index_entry(self, entry: IndexEntry) -> IndexEntry:
        normalized = self._normalize_trackable_path(entry.path)
        if normalized != entry.path.replace("\\", "/"):
            raise InvalidPath(entry.path)
        return IndexEntry(normalized, entry.oid, entry.mode)

    def _validate_tree_path(self, path: str) -> str:
        if "\\" in path:
            raise InvalidPath(path)
        normalized = self._normalize_trackable_path(path)
        if normalized != path:
            raise InvalidPath(path)
        return normalized

    def index_entries(self) -> list[IndexEntry]:
        with self._connect() as conn:
            rows = conn.execute("select path, oid, mode from index_entries order by path").fetchall()
        return [
            self._validate_index_entry(IndexEntry(row["path"], ObjectId(row["oid"]), row["mode"]))
            for row in rows
        ]

    def create_commit(
        self,
        *,
        message: str,
        author: Identity | None = None,
        committer: Identity | None = None,
        branch: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Commit:
        branch = branch or self.current_branch
        author = author or self.git_identity()
        committer = committer or author
        tree = self.build_tree(self.index_entries())
        parent = self.ref(branch)
        parents = [parent] if parent else []
        commit = Commit.create(
            tree=tree.id,
            parents=parents,
            author=author,
            committer=committer,
            message=message,
        )
        self.put_object(commit.id, commit.canonical())
        self.set_ref(branch, commit.id)
        if labels:
            with self._connect() as conn:
                for key, value in labels.items():
                    conn.execute(
                        "insert into labels(commit_oid, key, value) values (?, ?, ?)",
                        (str(commit.id), key, value),
                    )
        return commit

    def create_commit_from_entries(
        self,
        *,
        entries: Iterable[IndexEntry],
        message: str,
        branch: str,
        author: Identity | None = None,
        committer: Identity | None = None,
        labels: dict[str, str] | None = None,
    ) -> Commit:
        author = author or self.git_identity()
        committer = committer or author
        tree = self.build_tree(entries)
        parent = self.ref(branch)
        parents = [parent] if parent else []
        commit = Commit.create(
            tree=tree.id,
            parents=parents,
            author=author,
            committer=committer,
            message=message,
        )
        self.put_object(commit.id, commit.canonical())
        self.set_ref(branch, commit.id)
        if labels:
            with self._connect() as conn:
                for key, value in labels.items():
                    conn.execute(
                        "insert into labels(commit_oid, key, value) values (?, ?, ?)",
                        (str(commit.id), key, value),
                    )
        return commit

    def git_identity(self) -> Identity:
        name = (
            os.environ.get("GIT_AUTHOR_NAME")
            or os.environ.get("GIT_COMMITTER_NAME")
            or self._git_config_value("user.name")
            or "Trunks"
        )
        email = (
            os.environ.get("GIT_AUTHOR_EMAIL")
            or os.environ.get("GIT_COMMITTER_EMAIL")
            or self._git_config_value("user.email")
            or "trunks@local"
        )
        return Identity(name, email, datetime.now(UTC))

    def _git_config_value(self, key: str) -> str | None:
        import subprocess

        try:
            result = subprocess.run(
                ["git", "config", "--get", key],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "TRUNKS_BYPASS": "1"},
            )
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def build_tree(self, entries: Iterable[IndexEntry]) -> Tree:
        nested: dict[str, object] = {}
        for entry in entries:
            entry = self._validate_index_entry(entry)
            cursor = nested
            parts = entry.path.split("/")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})  # type: ignore[assignment]
            cursor[parts[-1]] = entry
        return self._write_tree_node(nested)

    def _write_tree_node(self, node: dict[str, object]) -> Tree:
        entries: list[TreeEntry] = []
        for name, value in sorted(node.items()):
            if isinstance(value, IndexEntry):
                entries.append(TreeEntry(value.mode, name, value.oid))
            else:
                child = self._write_tree_node(value)  # type: ignore[arg-type]
                entries.append(TreeEntry("40000", name, child.id))
        tree = Tree.from_entries(entries)
        self.put_object(tree.id, tree.canonical())
        return tree

    def put_object(self, oid: ObjectId, data: bytes) -> None:
        if ObjectId.from_bytes(data) != oid:
            raise ValueError("object id does not match object data")
        kind, _ = parse_canonical_object(data)
        with self._connect() as conn:
            conn.execute(
                "insert or ignore into objects(oid, kind, data) values (?, ?, ?)",
                (str(oid), kind, data),
            )

    def object_data(self, oid: ObjectId) -> bytes:
        with self._connect() as conn:
            row = conn.execute("select data from objects where oid = ?", (str(oid),)).fetchone()
        if row is None:
            raise ObjectNotFound(str(oid))
        return row["data"]

    def object_kind(self, oid: ObjectId) -> str:
        with self._connect() as conn:
            row = conn.execute("select kind from objects where oid = ?", (str(oid),)).fetchone()
        if row is None:
            raise ObjectNotFound(str(oid))
        return row["kind"]

    def blob_payload(self, oid: ObjectId) -> bytes:
        kind, payload = parse_canonical_object(self.object_data(oid))
        if kind != "blob":
            raise ValueError(f"expected blob, got {kind}")
        return payload

    def tree(self, oid: ObjectId) -> Tree:
        kind, payload = parse_canonical_object(self.object_data(oid))
        if kind != "tree":
            raise ValueError(f"expected tree, got {kind}")
        return parse_tree_payload(payload)

    def commit_object(self, oid: ObjectId) -> Commit:
        return self.load_commit(oid)

    def load_commit(self, oid: ObjectId) -> Commit:
        kind, payload = parse_canonical_object(self.object_data(oid))
        if kind != "commit":
            raise ValueError(f"expected commit, got {kind}")
        return parse_commit_payload(payload, oid)

    def resolve_path(self, tree_oid: ObjectId, path: str) -> ObjectId | None:
        parts = self._normalize_trackable_path(path).split("/")
        current = self.tree(tree_oid)
        for index, part in enumerate(parts):
            entry = next((entry for entry in current.entries if entry.name == part), None)
            if entry is None:
                return None
            if index == len(parts) - 1:
                return entry.oid
            current = self.tree(entry.oid)
        return None

    def ref(self, name: str) -> ObjectId | None:
        with self._connect() as conn:
            row = conn.execute("select oid from refs where name = ?", (normalize_ref(name),)).fetchone()
        return ObjectId(row["oid"]) if row else None

    def set_ref(self, name: str, oid: ObjectId) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into refs(name, oid) values (?, ?) on conflict(name) do update set oid = excluded.oid",
                (normalize_ref(name), str(oid)),
            )

    def cas_ref(self, name: str, expected: ObjectId | None, new: ObjectId) -> bool:
        normalized = normalize_ref(name)
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select oid from refs where name = ?", (normalized,)).fetchone()
            current = ObjectId(row["oid"]) if row else None
            if current != expected:
                return False
            if row is None:
                conn.execute("insert into refs(name, oid) values (?, ?)", (normalized, str(new)))
            else:
                conn.execute("update refs set oid = ? where name = ?", (str(new), normalized))
            return True

    def delete_ref(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from refs where name = ?", (normalize_ref(name),))

    def list_refs(self) -> list[tuple[str, ObjectId]]:
        return [(name, ObjectId(raw_oid)) for name, raw_oid in self._raw_refs()]

    def _raw_refs(self) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("select name, oid from refs order by name").fetchall()
        return [(row["name"], row["oid"]) for row in rows]

    def create_branch(self, name: str, *, start: str | ObjectId | None = None, switch: bool = True) -> None:
        if isinstance(start, ObjectId):
            oid = start
        elif start:
            oid = self.ref(start)
        else:
            oid = self.ref(self.current_branch)
        if oid is None:
            raise RefConflict("cannot create branch before first commit")
        self.set_ref(name, oid)
        if switch:
            self.checkout(name)

    def checkout(self, branch: str) -> None:
        oid = self.ref(branch)
        if oid is None:
            raise RefConflict(f"unknown branch: {branch}")
        old_entries = self.index_entries()
        self.set_current_branch(branch_name(normalize_ref(branch)))
        self.reset_index_to_commit(oid)
        self.checkout_tree(oid, previous_entries=old_entries)

    def reset_index_to_commit(self, commit_oid: ObjectId) -> None:
        commit = self.load_commit(commit_oid)
        entries = self.flatten_tree(commit.tree)
        with self._connect() as conn:
            conn.execute("delete from index_entries")
            for entry in entries:
                conn.execute(
                    "insert into index_entries(path, oid, mode) values (?, ?, ?)",
                    (entry.path, str(entry.oid), entry.mode),
                )

    def flatten_tree(self, tree_oid: ObjectId, prefix: str = "") -> list[IndexEntry]:
        tree = self.tree(tree_oid)
        results: list[IndexEntry] = []
        for entry in tree.entries:
            if "/" in entry.name or "\\" in entry.name:
                raise InvalidPath(entry.name)
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            path = self._validate_tree_path(path)
            if entry.mode == "40000":
                results.extend(self.flatten_tree(entry.oid, path))
            else:
                results.append(IndexEntry(path, entry.oid, entry.mode))
        return results

    def files_for_commit(self, commit_oid: ObjectId | None) -> list[ChangedFile]:
        if commit_oid is None:
            return []
        commit = self.load_commit(commit_oid)
        return [
            ChangedFile(entry.path, self.blob_payload(entry.oid))
            for entry in self.flatten_tree(commit.tree)
        ]

    def files_for_branch(self, branch: str) -> list[ChangedFile]:
        return self.files_for_commit(self.ref(branch))

    def commit_session_changes(
        self,
        *,
        branch: str,
        changes: Iterable[ChangedFile],
        message: str,
        labels: dict[str, str] | None = None,
    ) -> Commit | None:
        head = self.ref(branch)
        entries = {entry.path: entry for entry in self.flatten_tree(self.load_commit(head).tree)} if head else {}
        changed = False
        for item in changes:
            normalized = self._normalize_trackable_path(item.path)
            if item.data is None:
                changed = normalized in entries or changed
                entries.pop(normalized, None)
                continue
            blob = Blob.from_data(item.data)
            self.put_object(blob.id, blob.canonical())
            previous = entries.get(normalized)
            changed = previous is None or previous.oid != blob.id or changed
            entries[normalized] = IndexEntry(normalized, blob.id, "100644")
        if not changed:
            return None
        return self.create_commit_from_entries(
            entries=entries.values(),
            message=message,
            branch=branch,
            labels=labels,
        )

    def checkout_tree(self, commit_oid: ObjectId, *, previous_entries: list[IndexEntry] | None = None) -> None:
        commit = self.load_commit(commit_oid)
        target_entries = {entry.path: entry for entry in self.flatten_tree(commit.tree)}
        tracked = previous_entries if previous_entries is not None else self.index_entries()
        for entry in tracked:
            if entry.path not in target_entries:
                path = self._safe_worktree_file_path(entry.path)
                if path.exists() and path.is_file():
                    path.unlink()
        for entry in target_entries.values():
            path = self._safe_worktree_file_path(entry.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.blob_payload(entry.oid))

    def status(self, *, compare_worktree: bool = False) -> Status:
        index = self.index_entries()
        untracked: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []
        if compare_worktree:
            untracked, modified, deleted = self._worktree_status(index)
        dirty = bool(untracked or modified or deleted)
        return Status(
            branch=self.current_branch,
            head=self.ref(self.current_branch),
            indexed=len(index),
            dirty=dirty,
            backend=self.backend_url(),
            untracked=tuple(sorted(untracked)),
            modified=tuple(sorted(modified)),
            deleted=tuple(sorted(deleted)),
        )

    def _worktree_status(
        self, index: list[IndexEntry]
    ) -> tuple[list[str], list[str], list[str]]:
        # Stat-cache fast path: first compare stable file metadata against the
        # cached snapshot for paths we've seen before; only hash files whose
        # stat changed or that are new. Cuts per-file SHA-1 cost on warm runs.
        cache = self._read_status_stat_cache()
        index_by_path = {entry.path: entry.oid for entry in index}
        seen: set[str] = set()
        untracked: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []
        new_cache: dict[str, dict[str, object]] = {}

        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                rel = self._relative_worktree_path(path.resolve(strict=False))
            except InvalidPath:
                continue
            if is_internal_path(rel) or is_ignored(rel, self.root):
                continue
            try:
                normalised = self._normalize_trackable_path(rel)
            except InvalidPath:
                continue
            seen.add(normalised)

            try:
                stat = path.stat()
            except OSError:
                continue
            stat_key = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "ino": stat.st_ino,
                "mode": stat.st_mode,
            }
            cached = cache.get(normalised)
            indexed_oid = index_by_path.get(normalised)

            if (
                cached is not None
                and cached.get("size") == stat_key["size"]
                and cached.get("mtime_ns") == stat_key["mtime_ns"]
                and cached.get("ctime_ns") == stat_key["ctime_ns"]
                and cached.get("ino") == stat_key["ino"]
                and cached.get("mode") == stat_key["mode"]
                and "oid" in cached
            ):
                content_oid_value = str(cached["oid"])
            else:
                content_oid_value = str(Blob.from_data(path.read_bytes()).id)
            new_cache[normalised] = {**stat_key, "oid": content_oid_value}

            if indexed_oid is None:
                untracked.append(normalised)
            elif str(indexed_oid) != content_oid_value:
                modified.append(normalised)

        for indexed_path in index_by_path:
            if indexed_path not in seen:
                deleted.append(indexed_path)

        self._write_status_stat_cache(new_cache)
        return untracked, modified, deleted

    def _status_stat_cache_path(self) -> Path:
        return self.path.parent / f".{self.path.name}.status-stat.json"

    def _read_status_stat_cache(self) -> dict[str, dict[str, object]]:
        path = self._status_stat_cache_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _write_status_stat_cache(self, cache: dict[str, dict[str, object]]) -> None:
        path = self._status_stat_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def all_objects(self) -> list[tuple[ObjectId, bytes]]:
        with self._connect() as conn:
            rows = conn.execute("select oid, data from objects order by oid").fetchall()
        return [(ObjectId(row["oid"]), row["data"]) for row in rows]

    def reachable_objects(self) -> set[ObjectId]:
        roots = [oid for _, oid in self.list_refs()]
        seen: set[ObjectId] = set()
        stack = list(roots)
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            try:
                kind, payload = parse_canonical_object(self.object_data(oid))
            except ObjectNotFound:
                continue
            if kind == "commit":
                commit = parse_commit_payload(payload, oid)
                stack.append(commit.tree)
                stack.extend(commit.parents)
            elif kind == "tree":
                stack.extend(entry.oid for entry in parse_tree_payload(payload).entries)
        return seen

    def check_integrity(self) -> list[str]:
        errors: list[str] = []
        stack: list[ObjectId] = []
        seen: set[ObjectId] = set()
        for name, raw_oid in self._raw_refs():
            try:
                oid = ObjectId(raw_oid)
            except ValueError:
                errors.append(f"invalid ref target: {name} -> {raw_oid}")
                continue
            stack.append(oid)
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            try:
                data = self.object_data(oid)
            except ObjectNotFound:
                errors.append(f"missing object: {oid}")
                continue
            try:
                kind, payload = parse_canonical_object(data)
            except Exception as exc:
                errors.append(f"corrupt object: {oid} ({exc})")
                continue
            if ObjectId.from_bytes(data) != oid:
                errors.append(f"object id mismatch: {oid}")
                continue
            try:
                if kind == "commit":
                    commit = parse_commit_payload(payload, oid)
                    stack.append(commit.tree)
                    stack.extend(commit.parents)
                elif kind == "tree":
                    for entry in parse_tree_payload(payload).entries:
                        if "/" in entry.name or "\\" in entry.name:
                            raise InvalidPath(entry.name)
                        self._validate_tree_path(entry.name)
                        stack.append(entry.oid)
            except Exception as exc:
                errors.append(f"corrupt {kind} object: {oid} ({exc})")
        return errors

    def clean_unreachable(self) -> int:
        errors = self.check_integrity()
        if errors:
            raise RepositoryCorrupt("repository failed check; run trunks check before cleaning")
        reachable = {str(oid) for oid in self.reachable_objects()}
        with self._connect() as conn:
            rows = conn.execute("select oid from objects").fetchall()
            stale = [row["oid"] for row in rows if row["oid"] not in reachable]
            for oid in stale:
                conn.execute("delete from objects where oid = ?", (oid,))
        return len(stale)

    def backend_ref(self, name: str) -> ObjectId | None:
        with self._connect() as conn:
            row = conn.execute("select oid from backend_refs where name = ?", (normalize_ref(name),)).fetchone()
        return ObjectId(row["oid"]) if row else None

    def set_backend_ref(self, name: str, oid: ObjectId) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into backend_refs(name, oid) values (?, ?) on conflict(name) do update set oid = excluded.oid",
                (normalize_ref(name), str(oid)),
            )

    def record_push_transaction(
        self,
        *,
        tx_id: str,
        source_ref: str,
        expected_remote_ref: ObjectId | None,
        result_ref: ObjectId,
        object_ids: list[ObjectId],
        status: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into push_transactions(id, source_ref, expected_remote_ref, result_ref, object_ids, status) "
                "values (?, ?, ?, ?, ?, ?) on conflict(id) do update set status = excluded.status",
                (
                    tx_id,
                    normalize_ref(source_ref),
                    str(expected_remote_ref) if expected_remote_ref else None,
                    str(result_ref),
                    json.dumps([str(oid) for oid in object_ids]),
                    status,
                ),
            )

    def append_journal(self, entry: JournalEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert or ignore into journals(id, kind, data) values (?, ?, ?)",
                (entry.id, entry.kind, entry.to_json()),
            )
