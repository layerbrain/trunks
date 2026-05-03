# Actions

Trunks Actions lets agents run CI/CD at scale with GitHub-compatible workflows, fast-starting compute, provider failover, GPU-ready jobs, and replayable results without depending on GitHub Actions.

This page is the full reference. For the short walkthrough, see [trunks/actions/README.md](../trunks/actions/README.md).

GitHub Actions keeps runners waiting for work. Trunks Actions starts compute when work exists.

| | GitHub Actions | Trunks Actions |
|---|---|---|
| Basic model | Keep runner machines ready for GitHub jobs. | Start compute for the job, store the result, shut it down. |
| Control plane | GitHub owns the queue, scheduler, runner assignment, logs, and status. | Trunks storage owns the queue, leases, logs, artifacts, and status. |
| GPUs | Keep GPU runners alive, or build your own autoscaler. | Request GPUs in the job spec and route to GPU-capable compute. |
| Cost | Idle runners still cost money. Idle GPU runners cost a lot. | No idle runner fleet by default. Pay for the run, not the wait. |
| Failure boundary | Self-hosted runners still wait on GitHub Actions. | If one provider fails, route the job somewhere else. |

The result is simple: GitHub self-hosted runners are machines waiting for GitHub. Trunks Actions is disposable compute for each job.

## Git Push Starts Workflows

Put GitHub Actions-style workflow files under `.trunks/workflows/*.yml`:

```yaml
name: CI
on: [push, workflow_dispatch]
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - run: python3.12 -m unittest
```

Commit and push through the Trunks git shim:

```bash
git add .
git commit -m "Add CI"
git push
```

`git push` publishes the Trunks refs and starts every workflow that listens to `push`. GitHub is not the scheduler, runner registry, log store, or control plane.

Inspect workflows and workflow runs:

```bash
trunks actions workflows --json
trunks actions workflows lint --json
trunks actions workflow-runs --json
trunks actions workflow-runs --id <workflow-run-id> jobs --json
trunks actions workflow-runs --id <workflow-run-id> graph --json
trunks actions workflow-runs --id <workflow-run-id> logs --json
```

Move an existing GitHub workflow into Trunks:

```bash
trunks actions migrate .github/workflows/ci.yml
```

Strict mode fails closed for unsupported `uses:`, `environment:`, and `permissions.id-token: write` unless the required Trunks bridge exists.

## Run A Job Directly

```bash
trunks actions run --command "python3.12 -m unittest" --json
```

Manual runs are for debugging, SDK callers, and agent-created jobs that are not tied to a workflow file. The normal CI path is still `git push`.

## Queue And Execute

```bash
trunks actions enqueue --command "python3.12 -m unittest" --timeout 1800 --artifact reports --json
trunks actions execute --id executor-1 --json
```

`enqueue` creates a pending run and stores the execution request: command, commit, timeout, spec, isolation, provider preference, region preference, network mode, and artifact paths. `execute` claims one pending run with a CAS-fenced lease, resolves secrets, executes it through the selected sandbox provider, collects artifacts, and completes the run only if the lease token still matches.

## Lifecycle Safety

Core Trunks storage is multi-host safe: push uploads objects first, then advances the backend ref with compare-and-swap. Concurrent pushes to the same branch have one winner, and concurrent pushes to different branches can both succeed. The backend contract exercises this against memory, local disk, fileshare, SQLite, and the live backend suites for S3-compatible storage, Postgres, GCS, Azure Blob, and SFTP.

Actions run state is fenced with CAS in Trunks storage. If a repo has a primary storage backend, Actions refs and objects live under `refs/actions/repos/<repo>/...`; otherwise they fall back to the local repository database. Multiple executors pointed at the same storage backend can claim from the same queue without both completing the same run. Executors heartbeat while a sandbox is running, and a canceled running job stops the sandbox before returning.

Branch refs and Actions refs are separate namespaces, but both use Trunks storage when a storage backend is configured. `git push` advances branch refs and triggers matching workflows; queued runs, leases, logs, artifacts, capacity, and indexes live under `refs/actions/repos/<repo>/...`. Independent hosts participate in one global Actions queue when they point at the same configured Trunks storage backend.

The implemented providers are local process, Docker-on-the-same-host, and hosted Python providers such as Daytona, DigitalOcean, and Prime Intellect. Hosted providers receive a workspace through the sandbox upload protocol: `worktree` uploads trackable files, and commit-pinned runs materialize the requested Trunks commit before upload.

## Read Results

```bash
trunks actions list --json
trunks actions list --status succeeded --json
trunks actions describe --id <run-id> --json
trunks actions status --id <run-id> --json
trunks actions logs --id <run-id>
trunks actions watch --id <run-id>
trunks actions cancel --id <run-id> --json
```

The list commands read derived index refs instead of scanning every run ref.

`watch` streams state changes and logs from Trunks refs while an executor is still running. It does not require an Actions server.

## Artifacts

```bash
trunks actions run --command "npm test" --artifact coverage --json
trunks actions artifacts --id <run-id> --json
trunks actions artifacts --id <run-id> get coverage/index.html -o coverage.html
```

Artifacts are stored as Trunks content-addressed objects and referenced from the run.

## Secrets

```bash
trunks actions secrets bind NPM_TOKEN env:NPM_TOKEN
trunks actions secrets list --json
trunks actions secrets show NPM_TOKEN --json
trunks actions secrets remove NPM_TOKEN --json
```

Trunks stores binding metadata only. Plaintext values are resolved from the source at execution time and injected into the sandbox environment. CLI responses expose the source type only, and Actions logs mask resolved secret values before they are stored.

## Capacity

```bash
trunks actions capacity --json
trunks actions capacity set local cpu1-mem1g-disk1g-arm64-nogpu-default max-concurrent 4 --json
trunks actions health --json
trunks actions index repair --json
```

Executors check configured capacity before claiming a pending run. Provider infrastructure failures are written to health refs and soft routing tries healthy matching providers first.

## Migration Bridges

GitHub Checks writeback mirrors completed Trunks results back to a GitHub PR display when explicitly configured:

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

## Retention And Cleanup

Action creation does not run cleanup on the request path. Runs, logs, artifacts, workflow-run state, and index refs are retained until a maintenance command prunes them.

```bash
trunks actions prune --older-than 30d --keep-last 100 --dry-run --json
trunks actions prune --older-than 30d --keep-last 100 --json
trunks check --clean
```

`trunks actions prune` removes terminal Actions refs and indexes. It never deletes pending or running work. `--dry-run` reports what would be deleted. `--clean-objects` can also run object cleanup, but production operators should usually run `trunks check --clean` as a separate low-priority maintenance task because object GC can be more expensive than deleting refs.

For a continuously running CI deployment, run prune from a scheduled maintenance executor or cron. Do not run it inline before returning an enqueue/run response.

## Architecture Diagrams

- [Actions lifecycle](actions/lifecycle.mmd)
- [Storage refs](actions/storage-refs.mmd)

## Specs

```bash
trunks actions run \
  --command "nvidia-smi" \
  --cpu 8 \
  --memory 32gb \
  --disk 100gb \
  --gpu h100 \
  --gpu-count 1 \
  --region us \
  --json
```

Specs describe the compute a job needs. Providers advertise the specs, regions, isolation modes, network modes, and GPU kinds they can actually satisfy. A provider that cannot satisfy a requested spec is skipped unless it was selected with `--strict-provider`.

## Sandbox Providers

```bash
trunks sandboxes providers --json
trunks sandboxes providers show local --json
trunks sandboxes providers test local
trunks sandboxes providers orphans daytona --json
trunks sandboxes providers cleanup daytona --dry-run --json
trunks sandboxes providers orphans digitalocean --json
trunks sandboxes providers cleanup digitalocean --dry-run --json
trunks sandboxes providers scaffold acme-fast -o ./providers
trunks sandboxes specs --provider local
trunks sandboxes regions --provider local
```

The built-in `local` provider is a development provider. It runs a local process and only advertises the default network mode because it cannot enforce network isolation.

If Docker is installed and the daemon is reachable, Trunks also discovers the `docker` provider:

```bash
trunks sandboxes providers test docker
trunks actions run --provider docker --strict-provider --isolation container --command "python3.12 -m unittest" --json
```

Docker is optional. Trunks does not require Docker to install or use the local process provider.

Hosted providers should be Python classes under `trunks/sandboxes/providers/*.py`. Use direct HTTP calls for REST APIs, SSH for VM execution when needed, and expose the same `SandboxProvider` and `Sandbox` protocol either way. Provider SDK dependencies are not required by Trunks.

DigitalOcean is a VM-backed provider. It uses DigitalOcean's REST API for Droplet and SSH-key lifecycle, then SSH for run/upload/download because Droplets do not expose a sandbox exec API. It is useful for CI/CD over general cloud compute, but it should not be marketed as a fast-start sandbox provider.

```bash
trunks sandboxes providers add digitalocean-main \
  --type digitalocean \
  --secret api_key=... \
  --set region=nyc3
trunks sandboxes providers show digitalocean --json
trunks sandboxes providers test digitalocean --live --json
trunks actions run --provider digitalocean --strict-provider --isolation vm --arch x86_64 --command "echo ok" --json
trunks sandboxes providers cleanup digitalocean --json
```

Prime Intellect is a GPU pod provider. It uses Prime Intellect's REST API for pod lifecycle and SSH for run/upload/download:

```bash
trunks sandboxes providers add primeintellect-main \
  --type primeintellect \
  --secret api_key=...
trunks sandboxes providers show primeintellect --json
trunks sandboxes providers test primeintellect --live --json
trunks actions run --provider primeintellect --strict-provider --isolation vm --gpu H100_80GB --gpu-count 1 --command "nvidia-smi" --json
trunks sandboxes providers cleanup primeintellect --dry-run --json
```

Every provider should pass:

```bash
trunks sandboxes providers test <provider-id>
```

The contract test creates a sandbox, hydrates it, uploads a file, runs a command, streams logs, downloads an artifact, and destroys the sandbox. Hosted providers should also support `orphans` and `cleanup` so live tests can prove no external machines were leaked.

## Testing Matrix

See [Actions Testing Matrix](actions/testing-matrix.md) for the CLI, SDK, provider, workflow, queueing, and adversarial edge cases covered by the test suite.

See [Actions Examples](actions/examples.md) for a minimal headless CI runner that consumes the watch API.
