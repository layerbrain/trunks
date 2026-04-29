from __future__ import annotations

import base64
import os
import shutil
from dataclasses import dataclass
from typing import Any

from . import _speed
from .errors import BackendUnavailable, InvalidPath, ObjectNotFound, RefConflict, RepositoryNotFound, TrunksError
from .gitcache import GitCache
from .ids import ObjectId
from .journal import JournalEntry
from .objects import Blob
from .paths import is_ignored, is_internal_path
from .protocol import (
    BACKEND_UNAVAILABLE,
    CONFLICT,
    FILE_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    PROTOCOL_VERSION_ERROR,
    REF_NOT_FOUND,
    REPO_NOT_FOUND,
)
from .repository import Repository
from .trunk import Trunk


JSON = dict[str, Any]


@dataclass(frozen=True)
class RpcError(Exception):
    code: int
    message: str
    data: JSON | None = None


async def dispatch(repo: Repository, request: JSON) -> JSON | None:
    request_id = request.get("id")
    try:
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise RpcError(INVALID_REQUEST, "invalid JSON-RPC request")
        method = request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        result = await _call(repo, method, params)
        if "id" not in request:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        if "id" not in request:
            return None
        error = _rpc_error(exc)
        payload: JSON = {"code": error.code, "message": error.message}
        if error.data is not None:
            payload["data"] = error.data
        return {"jsonrpc": "2.0", "id": request_id, "error": payload}


async def dispatch_line(repo: Repository, raw: bytes) -> bytes:
    try:
        request = _speed.loads(raw)
    except Exception:
        response = {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "parse error"}}
        return _speed.dumps(response) + b"\n"
    if not isinstance(request, dict):
        response = {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "invalid JSON-RPC request"}}
        return _speed.dumps(response) + b"\n"
    response = await dispatch(repo, request)
    return (_speed.dumps(response) + b"\n") if response is not None else b""


def is_shutdown_request(raw: bytes) -> bool:
    try:
        request = _speed.loads(raw)
    except Exception:
        return False
    return isinstance(request, dict) and request.get("method") == "daemon.shutdown"


async def _call(repo: Repository, method: str, params: JSON) -> JSON | list[JSON] | list[str] | bool | str | None:
    if method == "hello.handshake":
        version = _string(params, "protocolVersion", required=False)
        if version and version != PROTOCOL_VERSION:
            raise RpcError(
                PROTOCOL_VERSION_ERROR,
                "protocol version mismatch",
                {"server": PROTOCOL_VERSION, "client": version},
            )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "repository": repo.name,
            "root": str(repo.root),
        }
    if method == "runtime.status":
        return _runtime_status(repo)
    if method == "repo.status":
        state = repo.status(compare_worktree=bool(params.get("compareWorktree", True)))
        return {
            "repository": repo.name,
            "path": str(repo.root),
            "branch": state.branch,
            "head": str(state.head) if state.head else None,
            "backend": state.backend,
            "dirty": state.dirty,
        }
    if method == "fs.read":
        data = _read_worktree_file(repo, _string(params, "path"))
        return {"dataBase64": base64.b64encode(data).decode("ascii")}
    if method == "fs.write":
        _write_worktree_file(repo, _string(params, "path"), _bytes(params))
        return {"ok": True}
    if method == "fs.list":
        return _list_worktree_dir(repo, _string(params, "path", default=""))
    if method == "fs.exists":
        return _worktree_exists(repo, _string(params, "path"))
    if method == "fs.remove":
        _remove_worktree_path(repo, _string(params, "path"))
        return {"ok": True}
    if method == "fs.copy":
        _write_worktree_file(repo, _string(params, "dest"), _read_worktree_file(repo, _string(params, "source")))
        return {"ok": True}
    if method == "fs.move":
        source = _string(params, "source")
        _write_worktree_file(repo, _string(params, "dest"), _read_worktree_file(repo, source))
        _remove_worktree_path(repo, source)
        return {"ok": True}
    if method == "fs.mkdir":
        _make_worktree_dir(repo, _string(params, "path"))
        return {"ok": True}
    if method == "vcs.checkpoint":
        return _checkpoint(repo, _string(params, "message", default="checkpoint"))
    if method == "vcs.diff":
        return _diff(repo, _string(params, "vs", default=repo.current_branch), params.get("from"))
    if method == "vcs.history":
        return _history(repo)
    if method == "vcs.push":
        async with Trunk(name=repo.name) as trunk:
            result = await trunk.push()
        return {
            "refsPushed": result.refs_pushed if result else 0,
            "refsAlreadyCurrent": result.refs_already_current if result else 0,
            "objectsUploaded": result.objects_uploaded if result else 0,
        }
    if method == "vcs.pull":
        async with Trunk(name=repo.name) as trunk:
            await trunk.pull()
        return {"ok": True}
    if method == "daemon.shutdown":
        return {"shutdown": True}
    raise RpcError(METHOD_NOT_FOUND, f"unknown method: {method}")


def _checkpoint(repo: Repository, message: str) -> JSON:
    repo.add_all_worktree_files(force=False)
    current = repo.ref(repo.current_branch)
    next_tree = repo.build_tree(repo.index_entries())
    if current is not None and repo.load_commit(current).tree == next_tree.id:
        return {"created": False, "head": str(current)}
    commit = repo.create_commit(message=message)
    GitCache(repo).rebuild()
    return {"created": True, "head": str(commit.id)}


def _diff(repo: Repository, base_name: str, from_ref: Any) -> list[JSON]:
    base = _entries_for_ref(repo, base_name)
    target = _entries_for_ref(repo, from_ref) if isinstance(from_ref, str) else _worktree_entries(repo)
    changes: list[JSON] = []
    for path in sorted(set(base) | set(target)):
        if path not in base:
            changes.append({"status": "A", "path": path})
        elif path not in target:
            changes.append({"status": "D", "path": path})
        elif base[path] != target[path]:
            changes.append({"status": "M", "path": path})
    return changes


def _history(repo: Repository) -> JSON:
    commits: dict[str, JSON] = {}
    stack = [oid for _, oid in repo.list_refs()]
    while stack:
        oid = stack.pop()
        if str(oid) in commits:
            continue
        commit = repo.load_commit(oid)
        commits[str(oid)] = {
            "id": str(commit.id),
            "tree": str(commit.tree),
            "parents": [str(parent) for parent in commit.parents],
            "message": commit.message,
        }
        stack.extend(commit.parents)
    return {
        "repository": repo.name,
        "currentBranch": repo.current_branch,
        "refs": {name: str(oid) for name, oid in repo.list_refs()},
        "commits": commits,
    }


def _entries_for_ref(repo: Repository, ref: str) -> dict[str, ObjectId]:
    oid = repo.ref(ref)
    if oid is None:
        try:
            oid = ObjectId(ref)
        except ValueError as exc:
            raise ObjectNotFound(ref) from exc
    return {entry.path: entry.oid for entry in repo.flatten_tree(repo.load_commit(oid).tree)}


def _worktree_entries(repo: Repository) -> dict[str, ObjectId]:
    entries: dict[str, ObjectId] = {}
    for path in sorted(repo.root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = repo._relative_worktree_path(path.resolve(strict=False))
        if is_internal_path(rel) or is_ignored(rel, repo.root):
            continue
        normalized = repo._normalize_trackable_path(rel)
        entries[normalized] = Blob.from_data(path.read_bytes()).id
    return entries


def _read_worktree_file(repo: Repository, path: str) -> bytes:
    normalized = repo._normalize_trackable_path(path)
    worktree_path = repo._safe_worktree_file_path(normalized)
    if worktree_path.is_file():
        return worktree_path.read_bytes()
    for entry in repo.index_entries():
        if entry.path == normalized:
            return repo.blob_payload(entry.oid)
    raise RpcError(FILE_NOT_FOUND, f"file not found: {normalized}", {"path": normalized})


def _write_worktree_file(repo: Repository, path: str, data: bytes) -> None:
    normalized = repo._normalize_trackable_path(path)
    worktree_path = repo._safe_worktree_file_path(normalized)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = repo.path.parent / "runtime" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{normalized.replace('/', '__')}.{os.getpid()}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, worktree_path)
    repo.write_file(normalized, data)
    blob = Blob.from_data(data)
    repo.append_journal(JournalEntry.create("worktree", {"op": "write", "path": normalized, "oid": str(blob.id)}))


def _remove_worktree_path(repo: Repository, path: str) -> None:
    normalized = repo._normalize_trackable_path(path)
    worktree_path = repo._safe_worktree_file_path(normalized)
    prefix = f"{normalized}/"
    tracked = [entry.path for entry in repo.index_entries() if entry.path == normalized or entry.path.startswith(prefix)]
    if not worktree_path.exists() and not tracked:
        raise RpcError(FILE_NOT_FOUND, f"file not found: {normalized}", {"path": normalized})
    if worktree_path.is_dir():
        shutil.rmtree(worktree_path)
    elif worktree_path.exists():
        worktree_path.unlink()
    for tracked_path in tracked or [normalized]:
        repo.delete_file(tracked_path)
    repo.append_journal(JournalEntry.create("worktree", {"op": "delete", "path": normalized}))


def _make_worktree_dir(repo: Repository, path: str) -> None:
    normalized = repo._normalize_trackable_path(path)
    repo._safe_worktree_file_path(normalized).mkdir(parents=True, exist_ok=True)


def _list_worktree_dir(repo: Repository, path: str = "") -> list[str]:
    normalized = "" if path in {"", ".", "/"} else repo._normalize_trackable_path(path)
    children = set(repo.list_dir(normalized or ""))
    directory = repo.root if not normalized else repo._safe_worktree_file_path(normalized)
    if directory.is_dir():
        for child in directory.iterdir():
            rel = repo._relative_worktree_path(child.resolve(strict=False))
            if is_internal_path(rel) or is_ignored(rel, repo.root):
                continue
            children.add(child.name)
    return sorted(children)


def _worktree_exists(repo: Repository, path: str) -> bool:
    if path in {"", ".", "/"}:
        return True
    normalized = repo._normalize_trackable_path(path)
    if repo._safe_worktree_file_path(normalized).exists():
        return True
    prefix = f"{normalized}/"
    return any(entry.path == normalized or entry.path.startswith(prefix) for entry in repo.index_entries())


def _runtime_status(repo: Repository) -> JSON:
    from . import runtime

    state = runtime.status(repo)
    return {
        "running": state.running,
        "pid": state.pid,
        "runtimePath": str(state.path),
        "socketPath": str(runtime.socket_path(repo)),
        "protocolVersion": PROTOCOL_VERSION,
    }


def _string(params: JSON, key: str, *, default: str | None = None, required: bool = True) -> str:
    value = params.get(key, default)
    if isinstance(value, str):
        return value
    if value is None and not required and default is None:
        return ""
    raise RpcError(INVALID_PARAMS, f"{key} must be a string")


def _bytes(params: JSON) -> bytes:
    if isinstance(params.get("dataBase64"), str):
        return base64.b64decode(params["dataBase64"])
    if isinstance(params.get("text"), str):
        return params["text"].encode("utf-8")
    raise RpcError(INVALID_PARAMS, "dataBase64 or text is required")


def _rpc_error(exc: Exception) -> RpcError:
    if isinstance(exc, RpcError):
        return exc
    if isinstance(exc, RepositoryNotFound):
        return RpcError(REPO_NOT_FOUND, str(exc))
    if isinstance(exc, ObjectNotFound):
        return RpcError(REF_NOT_FOUND, str(exc))
    if isinstance(exc, InvalidPath):
        return RpcError(INVALID_PARAMS, str(exc))
    if isinstance(exc, RefConflict):
        return RpcError(CONFLICT, str(exc))
    if isinstance(exc, BackendUnavailable):
        return RpcError(BACKEND_UNAVAILABLE, str(exc))
    if isinstance(exc, TrunksError):
        return RpcError(INTERNAL_ERROR, str(exc))
    return RpcError(INTERNAL_ERROR, str(exc))
