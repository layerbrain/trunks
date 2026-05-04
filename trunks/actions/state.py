from __future__ import annotations

from dataclasses import asdict, dataclass, field

from trunks.sandboxes import ExecResult, Isolation, LogChunk, Spec


RUN_SCHEMA = "trunks.actions.run.v1"
RUN_STATE_SCHEMA = "trunks.actions.run_state.v1"


@dataclass(frozen=True)
class RunState:
    phase: str
    token: str | None
    expires_at: int | None
    result_oid: str | None
    attempt: int
    executor: str | None
    provider: str | None
    spec_key: str
    requested_region: str | None
    region: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "_schema_version": RUN_STATE_SCHEMA,
            "phase": self.phase,
            "token": self.token,
            "expires_at": self.expires_at,
            "result_oid": self.result_oid,
            "attempt": self.attempt,
            "executor": self.executor,
            "provider": self.provider,
            "spec_key": self.spec_key,
            "requested_region": self.requested_region,
            "region": self.region,
        }


@dataclass(frozen=True)
class Run:
    id: str
    commit: str
    command: tuple[str, ...]
    spec: Spec
    timeout_s: int
    isolation: Isolation
    provider_id: str | None
    strict_provider: bool
    artifact_paths: tuple[str, ...]
    state: RunState
    logs: tuple[LogChunk, ...] = ()
    result: ExecResult | None = None
    artifacts: tuple[dict[str, object], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "_schema_version": RUN_SCHEMA,
            "object": "action_run",
            "id": self.id,
            "commit": self.commit,
            "command": list(self.command),
            "spec": asdict(self.spec),
            "timeout_s": self.timeout_s,
            "isolation": self.isolation,
            "provider_id": self.provider_id,
            "strict_provider": self.strict_provider,
            "artifact_paths": list(self.artifact_paths),
            "state": self.state.to_dict(),
            "logs": [asdict(log) for log in self.logs],
            "result": None if self.result is None else asdict(self.result),
            "artifacts": list(self.artifacts),
        }
