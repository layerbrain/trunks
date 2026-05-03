from __future__ import annotations

import itertools
import json
import os
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from trunks.ids import ulid
from trunks.objects import Blob
from trunks.repository import Repository
from trunks.sandboxes import GPU, Spec

from .backend_store import (
    store,
    workflow_run_index_by_repo_prefix,
    workflow_run_index_by_status_prefix,
    workflow_run_job_ref,
    workflow_run_state_ref,
)
from .run import run_command_async

WORKFLOW_SCHEMA = "trunks.actions.workflow.v1"
WORKFLOW_RUN_SCHEMA = "trunks.actions.workflow_run.v1"
MAX_MATRIX_COMBINATIONS = 256
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    run: str | None
    uses: str | None
    env: dict[str, str]
    with_args: dict[str, str]


@dataclass(frozen=True)
class WorkflowJob:
    id: str
    name: str
    needs: tuple[str, ...]
    steps: tuple[WorkflowStep, ...]
    spec: Spec
    matrix: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class Workflow:
    path: str
    name: str
    triggers: tuple[str, ...]
    jobs: tuple[WorkflowJob, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "_schema_version": WORKFLOW_SCHEMA,
            "object": "workflow",
            "path": self.path,
            "name": self.name,
            "triggers": list(self.triggers),
            "jobs": [
                {
                    "id": job.id,
                    "name": job.name,
                    "needs": list(job.needs),
                    "spec": asdict(job.spec),
                    "matrix": list(job.matrix),
                    "steps": [asdict(step) for step in job.steps],
                }
                for job in self.jobs
            ],
        }


def workflow_files(root: Path) -> list[Path]:
    workflows_dir = root / ".trunks" / "workflows"
    files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")) if workflows_dir.exists() else []
    shorthand = root / ".trunks" / "workflow.yml"
    if shorthand.exists() and not files:
        files.append(shorthand)
    return files


def load_workflows(root: Path, *, accept_best_effort: bool = False, oidc_enabled: bool = False) -> list[Workflow]:
    return [parse_workflow(path, root=root, accept_best_effort=accept_best_effort, oidc_enabled=oidc_enabled) for path in workflow_files(root)]


def parse_workflow(
    path: Path,
    *,
    root: Path | None = None,
    accept_best_effort: bool = False,
    oidc_enabled: bool = False,
) -> Workflow:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_WorkflowLoader)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"{path}: workflow must be a mapping")
    if "environment" in data:
        raise WorkflowError(f"{path}: environment is not supported by Trunks Actions")
    permissions = data.get("permissions")
    if isinstance(permissions, dict) and permissions.get("id-token") == "write" and not oidc_enabled:
        raise WorkflowError(f"{path}: permissions.id-token: write requires the Trunks OIDC bridge")
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, dict) or not jobs_raw:
        raise WorkflowError(f"{path}: workflow must define jobs")
    workflow_root = root or path.parent.parent.parent
    jobs = tuple(
        _parse_job(job_id, job_raw, path=path, accept_best_effort=accept_best_effort)
        for job_id, job_raw in jobs_raw.items()
    )
    _validate_needs(jobs, path)
    return Workflow(
        path=str(path.relative_to(workflow_root) if path.is_relative_to(workflow_root) else path),
        name=str(data.get("name") or path.stem),
        triggers=_parse_triggers(data.get("on")),
        jobs=jobs,
    )


async def run_workflow(
    repo: Repository,
    *,
    workflow: str | None = None,
    commit: str = "worktree",
    cwd: str | None = None,
    accept_best_effort: bool = False,
    oidc_enabled: bool = False,
) -> dict[str, object]:
    root = Path(cwd or repo.root)
    workflows = load_workflows(root, accept_best_effort=accept_best_effort, oidc_enabled=oidc_enabled)
    selected = _select_workflow(workflows, workflow)
    workflow_run = {
        "_schema_version": WORKFLOW_RUN_SCHEMA,
        "object": "workflow_run",
        "id": ulid(),
        "workflow": selected.to_dict(),
        "commit": commit,
        "phase": "running",
        "jobs": [],
    }
    _persist_workflow_run(repo, workflow_run)
    any_failed = False
    for stage in _stages(selected.jobs):
        for job in stage:
            for index, variables in enumerate(job.matrix or ({},)):
                command = _job_command(
                    job,
                    variables,
                    context={
                        "github.sha": commit,
                        "github.run_id": str(workflow_run["id"]),
                        "github.workflow": selected.name,
                        "github.job": job.id,
                    },
                )
                run = await run_command_async(
                    command,
                    commit=commit,
                    cwd=str(root),
                    spec=job.spec,
                    artifact_paths=_job_artifacts(job, variables),
                )
                payload = run.to_dict()
                phase = payload["state"]["phase"] if isinstance(payload["state"], dict) else "unknown"
                any_failed = any_failed or phase != "succeeded"
                workflow_run["jobs"].append(
                    {
                        "job": job.id,
                        "name": job.name,
                        "matrix_index": index,
                        "matrix": variables,
                        "run": payload["id"],
                        "phase": phase,
                    }
                )
                _persist_workflow_job_pointer(repo, str(workflow_run["id"]), job.id, str(payload["id"]), index=index)
        if any_failed:
            break
    workflow_run["phase"] = "failed" if any_failed else "succeeded"
    _persist_workflow_run(repo, workflow_run)
    return workflow_run


def lint_workflows(root: Path, *, accept_best_effort: bool = False, oidc_enabled: bool = False) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for path in workflow_files(root):
        try:
            workflow = parse_workflow(path, root=root, accept_best_effort=accept_best_effort, oidc_enabled=oidc_enabled)
        except WorkflowError as exc:
            results.append({"path": str(path.relative_to(root)), "valid": False, "error": str(exc)})
        else:
            results.append({"path": workflow.path, "valid": True, "workflow": workflow.to_dict()})
    return results


def migrate_workflow(source: Path, *, output: Path | None = None, dry_run: bool = False) -> dict[str, object]:
    target = output or Path(".trunks") / "workflows" / source.name
    payload = {
        "_schema_version": "trunks.actions.workflow_migration.v1",
        "object": "workflow_migration",
        "source": str(source),
        "target": str(target),
        "dry_run": dry_run,
    }
    if dry_run:
        return payload
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return payload


def load_workflow_run(repo: Repository, workflow_run: str) -> dict[str, object]:
    action_store = store(repo)
    oid = action_store.read_ref(workflow_run_state_ref(repo, workflow_run))
    if oid is None:
        raise KeyError(f"unknown workflow run {workflow_run!r}")
    return json.loads(action_store.read_blob_payload(oid).decode())


def list_workflow_runs(repo: Repository, *, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, object]]:
    action_store = store(repo)
    prefix = workflow_run_index_by_status_prefix(repo, status) if status else workflow_run_index_by_repo_prefix(repo)
    refs = action_store.list_refs(prefix)
    refs.sort(key=lambda item: item[0], reverse=True)
    return [json.loads(action_store.read_blob_payload(oid).decode()) for _name, oid in refs[offset : offset + limit]]


def workflow_run_jobs(repo: Repository, workflow_run: str) -> list[dict[str, object]]:
    payload = load_workflow_run(repo, workflow_run)
    jobs = payload.get("jobs")
    return jobs if isinstance(jobs, list) else []


def workflow_run_graph(repo: Repository, workflow_run: str) -> list[dict[str, str]]:
    payload = load_workflow_run(repo, workflow_run)
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        return []
    jobs = workflow.get("jobs")
    if not isinstance(jobs, list):
        return []
    edges: list[dict[str, str]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = job.get("id")
        needs = job.get("needs")
        if isinstance(job_id, str) and isinstance(needs, list):
            edges.extend({"from": str(need), "to": job_id} for need in needs)
    return edges


def workflow_run_logs(repo: Repository, workflow_run: str) -> list[dict[str, object]]:
    logs: list[dict[str, object]] = []
    for job in workflow_run_jobs(repo, workflow_run):
        if not isinstance(job, dict) or not isinstance(job.get("run"), str):
            continue
        from .storage import load_run

        run = load_run(repo, str(job["run"]))
        run_logs = run.get("logs")
        if isinstance(run_logs, list):
            logs.extend({**item, "job": job.get("job"), "run": job.get("run")} for item in run_logs if isinstance(item, dict))
    return logs


def _parse_job(job_id: str, raw: object, *, path: Path, accept_best_effort: bool) -> WorkflowJob:
    if not isinstance(raw, dict):
        raise WorkflowError(f"{path}: jobs.{job_id} must be a mapping")
    if "environment" in raw:
        raise WorkflowError(f"{path}: jobs.{job_id}.environment is not supported by Trunks Actions")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise WorkflowError(f"{path}: jobs.{job_id}.steps must be a non-empty list")
    steps = tuple(_parse_step(job_id, index, step, path=path, accept_best_effort=accept_best_effort) for index, step in enumerate(steps_raw))
    return WorkflowJob(
        id=job_id,
        name=str(raw.get("name") or job_id),
        needs=_parse_needs(raw.get("needs")),
        steps=steps,
        spec=_parse_spec(raw),
        matrix=_parse_matrix(raw),
    )


def _parse_step(job_id: str, index: int, raw: object, *, path: Path, accept_best_effort: bool) -> WorkflowStep:
    if not isinstance(raw, dict):
        raise WorkflowError(f"{path}: jobs.{job_id}.steps[{index}] must be a mapping")
    uses = raw.get("uses")
    run = raw.get("run")
    if uses is not None:
        use = str(uses)
        if _supported_uses(use):
            return WorkflowStep(
                name=str(raw.get("name") or use),
                run=None,
                uses=use,
                env=_env_map(raw.get("env"), path=path, location=f"jobs.{job_id}.steps[{index}].env"),
                with_args=_string_map(raw.get("with")),
            )
        if not accept_best_effort:
            raise WorkflowError(f"{path}: unsupported action {use!r}; use a run: step or pass --accept-best-effort")
    if run is None and uses is None:
        raise WorkflowError(f"{path}: jobs.{job_id}.steps[{index}] must define run or uses")
    return WorkflowStep(
        name=str(raw.get("name") or f"step-{index + 1}"),
        run=None if run is None else str(run),
        uses=None if uses is None else str(uses),
        env=_env_map(raw.get("env"), path=path, location=f"jobs.{job_id}.steps[{index}].env"),
        with_args=_string_map(raw.get("with")),
    )


def _supported_uses(value: str) -> bool:
    return value.startswith("actions/checkout@") or value.startswith("actions/upload-artifact@")


def _parse_needs(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return tuple(raw)
    raise WorkflowError("needs must be a string or list of strings")


def _parse_spec(raw: dict[object, object]) -> Spec:
    trunks = raw.get("trunks")
    spec_raw = trunks.get("spec") if isinstance(trunks, dict) else None
    if not isinstance(spec_raw, dict):
        return Spec(cpu=1, memory_gib=1, disk_gib=1, arch=_host_arch())
    gpu_raw = spec_raw.get("gpu")
    try:
        gpu = GPU(kind=str(gpu_raw.get("kind", gpu_raw.get("type", ""))), count=int(gpu_raw.get("count", 1))) if isinstance(gpu_raw, dict) else None
        return Spec(
            cpu=int(spec_raw.get("cpu", 1)),
            memory_gib=int(spec_raw.get("memory_gib", spec_raw.get("memory", 1))),
            disk_gib=int(spec_raw.get("disk_gib", spec_raw.get("disk", 1))),
            arch=str(spec_raw.get("arch", _host_arch())),  # type: ignore[arg-type]
            gpu=gpu,
            network=str(spec_raw.get("network", "default")),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"invalid trunks.spec: {exc}") from exc


def _parse_matrix(raw: dict[object, object]) -> tuple[dict[str, object], ...]:
    strategy = raw.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return ({},)
    keys = [str(key) for key in matrix.keys()]
    values: list[list[object]] = []
    for key in keys:
        raw_value = matrix.get(key)
        values.append(raw_value if isinstance(raw_value, list) else [raw_value])
    combinations = tuple(dict(zip(keys, item)) for item in itertools.product(*values))
    if len(combinations) > MAX_MATRIX_COMBINATIONS:
        raise WorkflowError(f"matrix expands to {len(combinations)} jobs; maximum is {MAX_MATRIX_COMBINATIONS}")
    return combinations


def _parse_triggers(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    if isinstance(raw, dict):
        return tuple(str(key) for key in raw)
    return ()


def _validate_needs(jobs: tuple[WorkflowJob, ...], path: Path) -> None:
    job_ids = {job.id for job in jobs}
    for job in jobs:
        missing = [need for need in job.needs if need not in job_ids]
        if missing:
            raise WorkflowError(f"{path}: jobs.{job.id}.needs references unknown jobs: {', '.join(missing)}")
    _stages(jobs)


def _stages(jobs: tuple[WorkflowJob, ...]) -> list[list[WorkflowJob]]:
    remaining = {job.id: job for job in jobs}
    completed: set[str] = set()
    stages: list[list[WorkflowJob]] = []
    while remaining:
        ready = [job for job in remaining.values() if set(job.needs).issubset(completed)]
        if not ready:
            raise WorkflowError("workflow jobs contain a needs cycle")
        ready.sort(key=lambda item: item.id)
        stages.append(ready)
        for job in ready:
            completed.add(job.id)
            del remaining[job.id]
    return stages


def _job_command(job: WorkflowJob, variables: dict[str, object], *, context: dict[str, str]) -> str:
    commands: list[str] = ["set -e"]
    for step in job.steps:
        if step.uses and _supported_uses(step.uses):
            continue
        for key, value in step.env.items():
            commands.append(f"export {key}={shlex.quote(_apply_expressions(value, variables, context))}")
        if step.run:
            commands.append(_apply_expressions(step.run, variables, context))
    return "\n".join(commands)


def _job_artifacts(job: WorkflowJob, variables: dict[str, object]) -> tuple[str, ...]:
    paths: list[str] = []
    for step in job.steps:
        if step.uses and step.uses.startswith("actions/upload-artifact@"):
            path = step.with_args.get("path")
            if path:
                paths.append(_apply_expressions(path, variables, {}))
    return tuple(paths)


def _apply_expressions(command: str, variables: dict[str, object], context: dict[str, str]) -> str:
    rendered = command
    for key, value in variables.items():
        rendered = rendered.replace(f"${{{{ matrix.{key} }}}}", str(value))
        rendered = rendered.replace(f"${{{{matrix.{key}}}}}", str(value))
    for key, value in context.items():
        rendered = rendered.replace(f"${{{{ {key} }}}}", value)
        rendered = rendered.replace(f"${{{{{key}}}}}", value)
    rendered = re.sub(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", r"$\1", rendered)
    return rendered


def _select_workflow(workflows: list[Workflow], name: str | None) -> Workflow:
    if not workflows:
        raise WorkflowError("no .trunks/workflows/*.yml or .trunks/workflow.yml files found")
    if name is None:
        return workflows[0]
    for workflow in workflows:
        if workflow.name == name or workflow.path == name or Path(workflow.path).stem == name:
            return workflow
    raise WorkflowError(f"unknown workflow {name!r}")


def _string_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _env_map(raw: object, *, path: Path, location: str) -> dict[str, str]:
    values = _string_map(raw)
    invalid = [key for key in values if _ENV_NAME.fullmatch(key) is None]
    if invalid:
        raise WorkflowError(f"{path}: {location} has invalid environment variable names: {', '.join(invalid)}")
    return values


def _host_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


def _persist_workflow_run(repo: Repository, payload: dict[str, object]) -> None:
    blob = Blob.from_data(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    action_store = store(repo)
    action_store.write_blob(blob)
    workflow_run = str(payload["id"])
    phase = str(payload["phase"])
    previous = action_store.read_ref(workflow_run_state_ref(repo, workflow_run))
    if previous is not None:
        previous_payload = json.loads(action_store.read_blob_payload(previous).decode())
        previous_phase = previous_payload.get("phase")
        if isinstance(previous_phase, str) and previous_phase != phase:
            action_store.delete_ref(f"{workflow_run_index_by_status_prefix(repo, previous_phase)}{workflow_run}")
    action_store.set_ref(workflow_run_state_ref(repo, workflow_run), blob.id)
    action_store.set_ref(f"{workflow_run_index_by_repo_prefix(repo)}{workflow_run}", blob.id)
    action_store.set_ref(f"{workflow_run_index_by_status_prefix(repo, phase)}{workflow_run}", blob.id)


def _persist_workflow_job_pointer(repo: Repository, workflow_run: str, job: str, run: str, *, index: int) -> None:
    blob = Blob.from_data(json.dumps({"job": job, "run": run, "matrix_index": index}, sort_keys=True).encode())
    action_store = store(repo)
    action_store.write_blob(blob)
    action_store.set_ref(workflow_run_job_ref(repo, workflow_run, job, index), blob.id)


class _WorkflowLoader(yaml.SafeLoader):
    pass


_WorkflowLoader.yaml_implicit_resolvers = {
    key: list(value)
    for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for first, resolvers in list(_WorkflowLoader.yaml_implicit_resolvers.items()):
    _WorkflowLoader.yaml_implicit_resolvers[first] = [
        (tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
