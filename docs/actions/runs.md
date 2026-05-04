# Runs

A run is one command, executed once, against one commit, on one sandbox. Workflows produce runs. CLI calls and the SDK produce runs. The shape is the same.

## One-shot

```bash
trunks actions run --command "python3.12 -m unittest" --json
```

For debugging, agent-driven jobs without a workflow file, and quick checks.

## Queue and execute

```bash
trunks actions enqueue --command "python3.12 -m unittest" --timeout 1800 --artifact reports --json
trunks actions execute --id executor-1 --json
```

`enqueue` writes a pending run to storage: command, commit, timeout, spec, isolation, provider preference, region preference, network mode, artifact paths. `execute` claims one pending run with a CAS-fenced lease, resolves repo secret bindings at execution time, runs it through the resolved provider, collects artifacts, and completes the run only if the lease token still matches.

Multiple executors pointed at the same Trunks storage share one queue. Two executors cannot complete the same run.

## Reading results

```bash
trunks actions list --json
trunks actions list --status succeeded --json
trunks actions describe --id <run-id> --json
trunks actions status --id <run-id> --json
trunks actions logs --id <run-id>
trunks actions watch --id <run-id>
trunks actions cancel --id <run-id> --json
```

`list` reads derived index refs. `watch` streams state changes and logs from refs while a run is in progress.

## Artifacts

```bash
trunks actions run --command "npm test" --artifact coverage --json
trunks actions artifacts --id <run-id> --json
trunks actions artifacts --id <run-id> get coverage/index.html -o coverage.html
```

Artifacts are stored as Trunks content-addressed objects. The run holds references to them. Identical files dedupe across runs.

## Lifecycle safety

Run state is fenced with CAS. Multiple executors against the same Trunks storage share one queue; two executors cannot complete the same run. Executors heartbeat while a sandbox runs. A canceled running job stops the sandbox before returning.

Branch refs and Actions refs are separate namespaces. `git push` advances branch refs and triggers matching workflows. Queued runs, leases, logs, artifacts, capacity, and indexes live under `refs/actions/repos/<repo>/...`.

## SDK

```python
from trunks.repository import Repository
from trunks.actions import (
    run_command, enqueue_command, execute_once, cancel_run,
    load_run, list_runs, get_artifact, write_artifact,
)

repo = Repository.find()
result = run_command("python3.12 -m unittest", commit="worktree")
```

`run_command` runs synchronously when called outside an event loop and returns an awaitable inside one. Same for `execute_once`. The SDK does not require a daemon.
