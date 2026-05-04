from __future__ import annotations

import json
from pathlib import Path

from trunks.ids import ObjectId
from trunks.objects import Blob
from trunks.repository import Repository

from .backend_store import run_artifact_ref, run_artifacts_prefix, store

ARTIFACT_SCHEMA = "trunks.actions.artifact.v1"


def collect_artifacts(repo: Repository, run: str, *, root: str | Path, paths: tuple[str, ...]) -> tuple[dict[str, object], ...]:
    base = Path(root).resolve()
    entries: list[dict[str, object]] = []
    for raw_path in paths:
        path = (base / raw_path).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"artifact path escapes repository root: {raw_path}")
        if path.is_dir():
            files = sorted(item for item in path.rglob("*") if item.is_file())
        elif path.is_file():
            files = [path]
        else:
            continue
        for file in files:
            resolved_file = file.resolve()
            if not resolved_file.is_relative_to(base):
                raise ValueError(f"artifact path escapes repository root: {raw_path}")
            relative = resolved_file.relative_to(base).as_posix()
            content = Blob.from_data(resolved_file.read_bytes())
            action_store = store(repo)
            action_store.write_blob(content)
            entry = {
                "_schema_version": ARTIFACT_SCHEMA,
                "object": "artifact",
                "run": run,
                "name": relative,
                "path": relative,
                "size": resolved_file.stat().st_size,
                "oid": content.id.value,
            }
            meta = Blob.from_data(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
            action_store.write_blob(meta)
            action_store.set_ref(run_artifact_ref(repo, run, relative), meta.id)
            entries.append(entry)
    return tuple(entries)


def list_artifacts(repo: Repository, run: str) -> list[dict[str, object]]:
    action_store = store(repo)
    refs = action_store.list_refs(run_artifacts_prefix(repo, run))
    refs.sort(key=lambda item: item[0])
    return [json.loads(action_store.read_blob_payload(oid).decode()) for _name, oid in refs]


def get_artifact(repo: Repository, run: str, name: str) -> bytes:
    for artifact in list_artifacts(repo, run):
        if artifact.get("name") == name or artifact.get("path") == name:
            oid = artifact.get("oid")
            if not isinstance(oid, str):
                raise KeyError(f"artifact has no object id: {name}")
            return store(repo).read_blob_payload(ObjectId(oid))
    raise KeyError(f"unknown artifact {name!r} for run {run!r}")


def write_artifact(repo: Repository, run: str, name: str, output: str | Path) -> None:
    Path(output).write_bytes(get_artifact(repo, run, name))
