# Trunks Actions

Trunks Actions lets agents run CI/CD at scale with GitHub-compatible workflows, fast-starting compute, provider failover, GPU-ready jobs, and replayable results without depending on GitHub Actions.

This README is the short walkthrough. The full command and API reference lives in [docs/actions.md](../../docs/actions.md).

GitHub Actions keeps runners waiting for work. Trunks Actions starts compute when work exists.

| | GitHub Actions | Trunks Actions |
|---|---|---|
| Basic model | Keep runner machines ready for GitHub jobs. | Start compute for the job, store the result, shut it down. |
| Control plane | GitHub owns the queue, scheduler, runner assignment, logs, and status. | Trunks storage owns the queue, leases, logs, artifacts, and status. |
| GPUs | Keep GPU runners alive, or build your own autoscaler. | Request GPUs in the job spec and route to GPU-capable compute. |
| Cost | Idle runners still cost money. Idle GPU runners cost a lot. | No idle runner fleet by default. Pay for the run, not the wait. |
| Failure boundary | Self-hosted runners still wait on GitHub Actions. | If one provider fails, route the job somewhere else. |

The result is simple: GitHub self-hosted runners are machines waiting for GitHub. Trunks Actions is disposable compute for each job.

## Lifecycle

| Step | What Happens |
|---|---|
| 1. Add workflow | Put CI/CD in `.trunks/workflows/*.yml`. |
| 2. Push normally | Run `git add .`, `git commit`, `git push`. |
| 3. Store the commit | Trunks writes Git objects and branch refs into your storage. |
| 4. Start the run | Workflows with `on: push` create Actions refs in the same storage. |
| 5. Pick compute | Trunks matches the job spec to local, Docker, hosted sandbox, VM, GPU, or private fleet compute. |
| 6. Run the job | The provider hydrates the repo, runs the steps, streams logs, and uploads artifacts. |
| 7. Save results | Logs, artifacts, status, and indexes are stored in Trunks refs and objects. |
| 8. Release compute | Ephemeral providers destroy the machine. Long-lived providers take the next job. |
| 9. Read anywhere | CLI, SDKs, apps, and dashboards read the same stored results. |

## Quick Start

Connect compute once:

```bash
trunks sandboxes providers add daytona-main \
  --type daytona \
  --secret api_key=...

trunks sandboxes providers test daytona --live --json
```

Move or create a workflow under `.trunks/workflows`:

```bash
trunks actions migrate .github/workflows/ci.yml
trunks actions workflows lint --json
```

Or write one directly:

```yaml
name: CI
on: [push, workflow_dispatch]
jobs:
  test:
    trunks:
      spec:
        cpu: 2
        memory_gib: 4
        disk_gib: 8
    steps:
      - uses: actions/checkout@v4
      - run: python3.12 -m unittest
```

Commit and push with Git like normal:

```bash
git add .
git commit -m "Add Trunks CI"
git push

trunks actions workflow-runs --json
```

When the Trunks git shim handles `git push`, it publishes the Trunks refs and automatically starts workflows with `on: push`. The workflow file is stored with the repo. The run state, logs, artifacts, queue, capacity, and indexes are stored in Trunks refs and objects. A long-running daemon, webhook producer, SDK call, or CLI can trigger the same run path; there is no GitHub Actions control plane required.

For a single command without a workflow:

```bash
trunks actions run --command "python3.12 -m unittest" --json
```

Queue work for any executor pointed at the same Trunks storage:

```bash
trunks actions enqueue --command "python3.12 -m unittest" --json
trunks actions execute --id executor-1 --json
```

Queued runs store the command, commit, timeout, spec, isolation, provider preference, region preference, network mode, and artifact paths. Executors resolve repo secret bindings at execution time, hydrate the requested worktree or commit into the sandbox, and collect configured artifacts before completing the run.

## Why Not GitHub Self-Hosted Runners?

GitHub self-hosted runners move execution onto your machine, but GitHub still owns the job. You register the machine, keep the runner process alive, keep labels correct, patch the host, monitor it, and wait for GitHub to assign work.

Trunks Actions moves the job into Trunks storage. Providers are just compute. A job asks for CPU, memory, disk, region, isolation, and GPUs. Trunks finds a matching provider, runs the job, stores the result, and destroys disposable compute when the provider supports it.

GitHub self-hosted runners ask: "Which registered machine is waiting for GitHub?"

Trunks Actions asks: "What compute does this job need right now?"

## Workflows

Move workflow files from `.github/workflows` to `.trunks/workflows`:

```bash
trunks actions migrate .github/workflows/ci.yml
trunks actions workflows lint --json
git add .
git commit -m "Move CI to Trunks"
git push
```

`trunks actions run` remains available for manual dispatch and debugging. It is not the normal CI entry point.

Supported workflow basics:

- `name`
- `on`
- `jobs`
- `needs`
- `strategy.matrix`
- `steps[].run`
- `actions/checkout`
- `actions/upload-artifact`
- step `env`
- `${{ matrix.* }}`
- `${{ github.sha }}`
- `${{ secrets.NAME }}`

Unsupported GitHub-hosted behavior fails closed.

## Results

```bash
trunks actions list --json
trunks actions describe --id <run-id> --json
trunks actions logs --id <run-id>
trunks actions artifacts --id <run-id> --json
trunks actions workflow-runs --json
trunks actions workflow-runs --id <workflow-run-id> jobs --json
```

Runs, logs, workflow runs, artifacts, capacity, and indexes are stored in Trunks refs and objects.

## Lifecycle Boundary

Core Trunks storage is multi-host safe through backend compare-and-swap on refs. Actions state now uses the configured Trunks storage namespace under `refs/actions/repos/<repo>/...`, so separate executor hosts that point at the same storage see the same queue, leases, logs, artifacts, and results. Local SQLite remains "shared storage of one" when no storage backend is configured.

Branch refs and Actions refs are separate namespaces in the same Trunks storage. `git push` through the Trunks shim publishes branch refs and objects, then starts matching workflows. Manual dispatch through `trunks actions enqueue` / `trunks actions run` writes Actions queue refs directly, and executors claim them with CAS fencing. Logs are stored as per-chunk refs rather than rewritten into one run-state blob.

## Secrets

```bash
trunks actions secrets bind NPM_TOKEN env:NPM_TOKEN
trunks actions secrets list --json
```

Trunks stores binding metadata only. Plaintext values are resolved at execution time and injected into the sandbox environment. CLI responses expose the source type only, and Actions logs mask resolved secret values before they are stored.

## Capacity

```bash
trunks actions capacity --json
trunks actions capacity set local <spec-key> max-concurrent 4 --json
trunks actions health --json
```

Executors check capacity before claiming pending runs.
Capacity is slot-leased in the shared pool namespace, so multiple executors cannot all consume the last slot for the same spec at the same time.
Provider infrastructure failures are attributed to the provider and recorded in shared health refs. Soft routing tries healthy matching providers first.

## Migration Bridges

GitHub Checks writeback is opt-in and pure HTTP. Set these variables when a repo is still reviewed on GitHub and Trunks should mirror green/red checks back to the PR:

```bash
export TRUNKS_GITHUB_CHECKS_TOKEN=...
export TRUNKS_GITHUB_REPOSITORY=owner/repo
export TRUNKS_ACTIONS_DETAILS_URL="https://trunks.example/runs/{run}"
```

OIDC tokens fail closed unless a signing key is configured:

```bash
export TRUNKS_OIDC_SIGNING_KEY=...
trunks actions oidc token --id <run-id> --audience aws --json
```

## Sandboxes

```bash
trunks sandboxes providers --json
trunks sandboxes providers test local
trunks sandboxes providers test docker
trunks sandboxes providers test daytona --live
trunks sandboxes providers test digitalocean --live
trunks sandboxes providers orphans daytona --json
trunks sandboxes providers orphans digitalocean --json
trunks sandboxes providers cleanup daytona --dry-run --json
trunks sandboxes providers cleanup digitalocean --dry-run --json
trunks sandboxes providers scaffold acme-fast -o ./providers
```

First-party hosted providers should be async-first Python classes under `trunks/sandboxes/providers/*.py`. Providers that expose HTTP exec, such as Daytona, call the REST API directly. Providers that provision VMs or pods, such as DigitalOcean and Prime Intellect, use the provider API for lifecycle and SSH for execution. Do not depend on provider SDK packages.

Docker is optional and only appears when Docker is installed and reachable. Hosted providers appear when connected with `trunks sandboxes providers add ... --secret api_key=...`. Provider-specific environment variables are accepted for ephemeral local testing or CI overrides.
