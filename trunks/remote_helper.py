"""Git remote helper for trunks storage backends.

Implements the gitremote-helpers stdio protocol so `git push trunks://...`
and `git fetch trunks://...` route refs and objects through any configured
trunks Backend (S3, MinIO, Postgres, GCS, Azure, SFTP, file, sqlite, ...).

URL schemes:
    trunks://<storage-name>/<repo-path>
        Resolves <storage-name> via ~/.trunks/config and namespaces the
        backend at <repo-path>.
    trunks+<scheme>://<rest>
        Inline storage URL - strips the `trunks+` prefix and hands the
        remainder to backend_from_url.

Real git owns .git/refs, .git/HEAD, .git/config. The helper only writes
objects through git plumbing and reads from there for push.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse, urlunparse

from .backend import Backend
from .config import get_storage_profile, load_storage_profiles
from .errors import ObjectNotFound
from .ids import ObjectId
from .objects import Blob, parse_canonical_object
from .refs import normalize_ref
from .storage import Storage
from .url import backend_from_storage, backend_from_url


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 2:
        print("usage: git-remote-trunks <remote-name> <url>", file=sys.stderr)
        return 128
    remote_name, raw_url = args
    git_bin = _find_real_git()
    if git_bin is None:
        print("fatal: git binary not found on PATH", file=sys.stderr)
        return 128
    git_dir = _detect_git_dir(git_bin)
    if git_dir is None:
        print("fatal: not in a git repository", file=sys.stderr)
        return 128
    try:
        return asyncio.run(_run(remote_name, raw_url, git_dir, git_bin))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"git-remote-trunks: {exc}", file=sys.stderr)
        return 1


def _find_real_git() -> str | None:
    override = os.environ.get("TRUNKS_REAL_GIT")
    if override and Path(override).exists():
        return override
    self_path = Path(sys.argv[0]).resolve() if sys.argv else None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "git"
        if not candidate.exists():
            continue
        if self_path and candidate.resolve() == self_path:
            continue
        if _looks_like_trunks_shim(candidate):
            continue
        return str(candidate)
    for fallback in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if Path(fallback).exists():
            return fallback
    return None


def _looks_like_trunks_shim(path: Path) -> bool:
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"trunks.gitshim" in head or b"TRUNKS_GIT_SHIM_DIR" in head


def _detect_git_dir(git_bin: str) -> Path | None:
    env = os.environ.get("GIT_DIR")
    if env:
        return Path(env).resolve()
    result = subprocess.run(
        [git_bin, "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _resolve_backend(raw_url: str) -> Backend:
    parsed = urlparse(raw_url)
    scheme = parsed.scheme
    if scheme == "trunks":
        storage_name = parsed.netloc
        repo_path = parsed.path.lstrip("/")
        if not storage_name:
            raise ValueError(f"trunks URL missing storage name: {raw_url}")
        if not repo_path:
            raise ValueError(f"trunks URL missing repo path: {raw_url}")
        profile = get_storage_profile(storage_name)
        if profile is None:
            raise ValueError(
                f"storage profile {storage_name!r} not found in ~/.trunks/config "
                f"(run: trunks storage add --name {storage_name} ...)"
            )
        backend = _backend_from_named_storage(profile, repo_path)
        if backend is None:
            raise ValueError(f"could not resolve backend for storage {storage_name!r}")
        return backend
    if scheme.startswith("trunks+"):
        inner = urlunparse((
            scheme.removeprefix("trunks+"),
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ))
        backend = backend_from_url(inner)
        if backend is None:
            raise ValueError(f"could not resolve backend for url {inner!r}")
        return backend
    raise ValueError(f"unsupported url scheme: {scheme!r}")


def _backend_from_named_storage(profile: Storage, repo_path: str) -> Backend | None:
    primary = backend_from_storage(profile, repo_path)
    if primary is None or profile.role != "primary":
        return primary
    mirrors: list[Backend] = []
    for mirror_profile in load_storage_profiles():
        if mirror_profile.name == profile.name or mirror_profile.role != "mirror":
            continue
        mirror = backend_from_storage(mirror_profile, repo_path)
        if mirror is not None:
            mirrors.append(mirror)
    if not mirrors:
        return primary
    from .backends.multi import Multi

    return Multi(primary=primary, mirrors=mirrors)


async def _run(remote_name: str, raw_url: str, git_dir: Path, git_bin: str) -> int:
    backend = _resolve_backend(raw_url)
    helper = Helper(
        remote_name=remote_name,
        raw_url=raw_url,
        backend=backend,
        git_dir=git_dir,
        git_bin=git_bin,
    )
    async with backend:
        await helper.loop()
    return 0


class Helper:
    """gitremote-helpers stdio protocol driver bound to a Backend."""

    def __init__(
        self,
        *,
        remote_name: str,
        raw_url: str,
        backend: Backend,
        git_dir: Path,
        git_bin: str,
    ) -> None:
        self.remote_name = remote_name
        self.raw_url = raw_url
        self.backend = backend
        self.git_dir = git_dir
        self.git_bin = git_bin
        self.options: dict[str, str] = {}
        self.repo_name = _repo_name_from_url(raw_url)

    async def loop(self) -> None:
        batch_kind: str | None = None
        batch: list[str] = []
        while True:
            raw_line = sys.stdin.readline()
            if raw_line == "":
                return
            line = raw_line.rstrip("\n").rstrip("\r")
            if line == "":
                if batch_kind == "fetch":
                    await self._respond_fetch(batch)
                elif batch_kind == "push":
                    await self._respond_push(batch)
                else:
                    return
                batch = []
                batch_kind = None
                continue
            cmd, _, rest = line.partition(" ")
            if cmd == "capabilities":
                self._write("fetch\npush\noption\n\n")
            elif cmd == "list":
                await self._respond_list()
            elif cmd == "option":
                self._respond_option(rest)
            elif cmd == "fetch":
                batch_kind = "fetch"
                batch.append(rest)
            elif cmd == "push":
                batch_kind = "push"
                batch.append(rest)
            else:
                self._write("\n")

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    async def _respond_list(self) -> None:
        emitted: list[tuple[str, str]] = []
        async for ref in self.backend.list_refs("refs/"):
            if ref.oid is None:
                continue
            if ref.name.startswith("refs/actions/"):
                continue
            emitted.append((ref.name, ref.oid.value))
        emitted.sort()
        head_oid = await self.backend.read_ref("HEAD")
        head_symref: str | None = None
        if head_oid is None and emitted:
            for name, _ in emitted:
                if name == "refs/heads/main":
                    head_symref = name
                    break
            if head_symref is None:
                for name, _ in emitted:
                    if name.startswith("refs/heads/"):
                        head_symref = name
                        break
        lines = [f"{oid_str} {name}" for name, oid_str in emitted]
        if head_symref is not None:
            lines.append(f"@{head_symref} HEAD")
        self._write("\n".join(lines) + "\n\n" if lines else "\n")

    def _respond_option(self, rest: str) -> None:
        key, _, value = rest.partition(" ")
        if not key:
            self._write("unsupported\n")
            return
        if key in {
            "progress",
            "verbosity",
            "check-connectivity",
            "cloning",
            "update-shallow",
            "pushcert",
            "force",
            "deepen",
            "depth",
        }:
            self.options[key] = value
            self._write("ok\n")
        elif key == "dry-run":
            self.options["dry-run"] = value
            self._write("ok\n")
        else:
            self._write("unsupported\n")

    async def _respond_fetch(self, specs: list[str]) -> None:
        for spec in specs:
            if not spec:
                continue
            sha_str, _, _name = spec.partition(" ")
            if not sha_str:
                continue
            await self._fetch_closure(ObjectId(sha_str))
        self._write("\n")

    async def _fetch_closure(self, head: ObjectId) -> None:
        seen: set[str] = set()
        queue: deque[ObjectId] = deque([head])
        raw_to_install: dict[ObjectId, bytes] = {}
        while queue:
            oid = queue.popleft()
            if oid.value in seen:
                continue
            seen.add(oid.value)
            if self._git_has_object(oid):
                raw = self._git_cat_file_raw(oid, allow_missing=True)
                if raw is None:
                    continue
                for child in _referenced_shas(raw):
                    queue.append(child)
                continue
            try:
                raw = await self.backend.read_object(oid)
            except ObjectNotFound as exc:
                raise RuntimeError(f"missing object in storage: {oid.value}") from exc
            raw_to_install[oid] = raw
            for child in _referenced_shas(raw):
                queue.append(child)
        if raw_to_install:
            self._install_objects_via_pack(raw_to_install)

    def _git_has_object(self, oid: ObjectId) -> bool:
        result = subprocess.run(
            [self.git_bin, "--git-dir", str(self.git_dir), "cat-file", "-e", oid.value],
            capture_output=True,
        )
        return result.returncode == 0

    def _install_objects_via_pack(self, objects: dict[ObjectId, bytes]) -> None:
        with tempfile.TemporaryDirectory(prefix="trunks-fetch-pack-") as tmp:
            temp_git_dir = Path(tmp) / ".git"
            subprocess.run(
                [self.git_bin, "init", "--bare", "-q", str(temp_git_dir)],
                capture_output=True,
                check=True,
            )
            for oid, raw in objects.items():
                kind, payload = parse_canonical_object(raw)
                result = subprocess.run(
                    [self.git_bin, "--git-dir", str(temp_git_dir), "hash-object", "-w", "-t", kind, "--stdin"],
                    input=payload,
                    capture_output=True,
                    check=True,
                )
                installed = result.stdout.decode("ascii").strip()
                if installed != oid.value:
                    raise RuntimeError(f"object hash mismatch while staging {oid.value}: got {installed}")
            pack = subprocess.run(
                [self.git_bin, "--git-dir", str(temp_git_dir), "pack-objects", "--stdout"],
                input=("\n".join(oid.value for oid in objects) + "\n").encode("ascii"),
                capture_output=True,
                check=True,
            )
            subprocess.run(
                [self.git_bin, "--git-dir", str(self.git_dir), "index-pack", "--stdin"],
                input=pack.stdout,
                capture_output=True,
                check=True,
            )

    async def _respond_push(self, specs: list[str]) -> None:
        responses: list[str] = []
        for spec in specs:
            if not spec:
                continue
            responses.append(await self._push_one(spec))
        self._write("\n".join(responses) + "\n\n" if responses else "\n")

    async def _push_one(self, spec: str) -> str:
        force = spec.startswith("+")
        body = spec[1:] if force else spec
        if ":" not in body:
            return f"error {spec} bad-spec"
        src, dst = body.split(":", 1)
        dst = normalize_ref(dst)
        if src == "":
            current = await self.backend.read_ref(dst)
            if current is None:
                return f"ok {dst}"
            try:
                await self.backend.delete_ref(dst)
            except NotImplementedError:
                return f"error {dst} delete-not-supported"
            return f"ok {dst}"
        try:
            new_oid = ObjectId(self._git_rev_parse(src))
        except RuntimeError as exc:
            return f"error {dst} {exc}"
        current = await self.backend.read_ref(dst)
        if current is not None and not force:
            if not self._is_ancestor(current, new_oid):
                return f"error {dst} non-fast-forward"
        if self.options.get("dry-run") == "true":
            return f"ok {dst}"
        excludes: list[str] = []
        if current is not None:
            excludes.append(current.value)
        async for ref in self.backend.list_refs("refs/"):
            if ref.oid is None or ref.name == dst or ref.name.startswith("refs/actions/"):
                continue
            excludes.append(ref.oid.value)
        try:
            objects = self._git_rev_list_objects(new_oid.value, excludes)
        except subprocess.CalledProcessError as exc:
            return f"error {dst} rev-list-failed: {exc.stderr.decode('utf-8', 'replace').strip()}"
        for oid in objects:
            if await self.backend.has_object(oid):
                continue
            raw = self._git_cat_file_raw(oid)
            if raw is None:
                return f"error {dst} object-missing-locally:{oid.value}"
            await self.backend.write_object(oid, raw)
        if not await self.backend.cas_ref(dst, current, new_oid):
            if force:
                fresh = await self.backend.read_ref(dst)
                if not await self.backend.cas_ref(dst, fresh, new_oid):
                    return f"error {dst} cas-conflict"
            else:
                return f"error {dst} non-fast-forward"
        await self._write_push_trigger(dst, new_oid)
        return f"ok {dst}"

    async def _write_push_trigger(self, ref: str, oid: ObjectId) -> None:
        if ref.startswith("refs/actions/"):
            return
        payload: dict[str, object] = {
            "_schema_version": "trunks.actions.push_trigger.v1",
            "object": "push_trigger",
            "repo": self.repo_name,
            "remote": self.remote_name,
            "ref": ref,
            "commit": oid.value,
            "pusher_id": _pusher_id(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        if ref.startswith("refs/heads/"):
            payload["branch"] = ref.removeprefix("refs/heads/")
        elif ref.startswith("refs/tags/"):
            payload["tag"] = ref.removeprefix("refs/tags/")
        blob = Blob.from_data(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        await self.backend.write_object(blob.id, blob.canonical())
        trigger_ref = f"refs/actions/repos/{self.repo_name}/triggers/push/{oid.value}"
        current = await self.backend.read_ref(trigger_ref)
        await self.backend.cas_ref(trigger_ref, current, blob.id)

    def _git_rev_parse(self, ref: str) -> str:
        result = subprocess.run(
            [self.git_bin, "--git-dir", str(self.git_dir), "rev-parse", "--verify", ref],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"could not resolve {ref!r}: {result.stderr.strip()}")
        return result.stdout.strip()

    def _is_ancestor(self, ancestor: ObjectId, descendant: ObjectId) -> bool:
        result = subprocess.run(
            [
                self.git_bin,
                "--git-dir",
                str(self.git_dir),
                "merge-base",
                "--is-ancestor",
                ancestor.value,
                descendant.value,
            ],
            capture_output=True,
        )
        return result.returncode == 0

    def _git_rev_list_objects(self, head: str, excludes: list[str]) -> list[ObjectId]:
        cmd = [self.git_bin, "--git-dir", str(self.git_dir), "rev-list", "--objects", head]
        for exclude in excludes:
            check = subprocess.run(
                [self.git_bin, "--git-dir", str(self.git_dir), "cat-file", "-e", exclude],
                capture_output=True,
            )
            if check.returncode == 0:
                cmd.append(f"^{exclude}")
        result = subprocess.run(cmd, capture_output=True, check=True)
        oids: list[ObjectId] = []
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if not line:
                continue
            sha = line.split(" ", 1)[0]
            oids.append(ObjectId(sha))
        return oids

    def _git_cat_file_raw(self, oid: ObjectId, *, allow_missing: bool = False) -> bytes | None:
        result = subprocess.run(
            [self.git_bin, "--git-dir", str(self.git_dir), "cat-file", "--batch"],
            input=(oid.value + "\n").encode("ascii"),
            capture_output=True,
        )
        if result.returncode != 0:
            if allow_missing:
                return None
            raise RuntimeError(f"cat-file failed for {oid.value}: {result.stderr!r}")
        out = result.stdout
        if not out:
            if allow_missing:
                return None
            raise RuntimeError(f"cat-file empty output for {oid.value}")
        try:
            nl = out.index(b"\n")
        except ValueError:
            if allow_missing:
                return None
            raise RuntimeError(f"cat-file malformed output for {oid.value}")
        header = out[:nl].decode("ascii", errors="replace")
        parts = header.split(" ")
        if len(parts) >= 2 and parts[1] == "missing":
            if allow_missing:
                return None
            raise RuntimeError(f"object missing locally: {oid.value}")
        if len(parts) < 3:
            raise RuntimeError(f"unexpected cat-file header: {header!r}")
        type_str, size_str = parts[1], parts[2]
        size = int(size_str)
        body = out[nl + 1 : nl + 1 + size]
        raw = f"{type_str} {size}".encode("ascii") + b"\x00" + body
        if hashlib.sha1(raw).hexdigest() != oid.value:
            raise RuntimeError(f"object hash mismatch for {oid.value}")
        return raw


def _referenced_shas(raw: bytes) -> Iterable[ObjectId]:
    nul = raw.index(b"\x00")
    header = raw[:nul].decode("ascii")
    body = raw[nul + 1 :]
    type_str = header.split(" ", 1)[0]
    if type_str == "commit":
        return _commit_refs(body)
    if type_str == "tree":
        return _tree_refs(body)
    if type_str == "tag":
        return _tag_refs(body)
    return ()


def _commit_refs(body: bytes) -> list[ObjectId]:
    refs: list[ObjectId] = []
    for line_bytes in body.split(b"\n"):
        if not line_bytes:
            break
        if line_bytes.startswith(b"tree "):
            refs.append(ObjectId(line_bytes[5:].strip().decode("ascii")))
        elif line_bytes.startswith(b"parent "):
            refs.append(ObjectId(line_bytes[7:].strip().decode("ascii")))
    return refs


def _tree_refs(body: bytes) -> list[ObjectId]:
    refs: list[ObjectId] = []
    pos = 0
    while pos < len(body):
        try:
            space = body.index(b" ", pos)
            nul = body.index(b"\x00", space)
        except ValueError:
            break
        sha_bytes = body[nul + 1 : nul + 21]
        if len(sha_bytes) != 20:
            break
        refs.append(ObjectId(sha_bytes.hex()))
        pos = nul + 21
    return refs


def _tag_refs(body: bytes) -> list[ObjectId]:
    for line_bytes in body.split(b"\n"):
        if not line_bytes:
            break
        if line_bytes.startswith(b"object "):
            return [ObjectId(line_bytes[7:].strip().decode("ascii"))]
    return []


def _repo_name_from_url(raw_url: str) -> str:
    override = os.environ.get("TRUNKS_REPO_NAME")
    if override:
        return _safe_ref_path(override)
    parsed = urlparse(raw_url)
    if parsed.scheme == "trunks":
        return _safe_ref_path(parsed.path.lstrip("/"))
    if parsed.scheme.startswith("trunks+"):
        if parsed.fragment:
            return _safe_ref_path(parsed.fragment)
        path = parsed.path.strip("/")
        return _safe_ref_path(Path(path).name if path else parsed.netloc)
    return "repo"


def _safe_ref_path(value: str) -> str:
    parts: list[str] = []
    for raw_part in value.split("/"):
        part = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_part.strip())
        part = part.replace("..", ".")
        part = part.strip(".")
        if part.endswith(".lock"):
            part = part[:-5]
        if part:
            parts.append(part)
    return "/".join(parts) or "repo"


def _pusher_id() -> str:
    return os.environ.get("TRUNKS_PUSHER_ID") or os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("USER") or "unknown"


if __name__ == "__main__":
    sys.exit(main())
