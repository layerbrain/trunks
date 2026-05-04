# Trunks Actions

Trunks Actions lets agents run CI/CD at scale with GitHub-compatible workflows, fast-starting compute, provider failover, GPU-ready jobs, and replayable results without depending on GitHub Actions.

## How it works

1. **You commit.** The commit goes to Trunks storage as a content-addressed snapshot.
2. **You enqueue a run.** A pending entry lands in the queue, also in Trunks storage. Any executor pointed at the same storage can pick it up.
3. **A sandbox boots.** The provider creates the machine. The Trunks virtual filesystem mounts the commit into the workspace. Files stream in lazily on first read, so a 5 GB monorepo starts instantly.
4. **The job runs.** Logs, artifacts, and final state write back to storage as they happen.
5. **You read results.** `trunks actions logs`, `describe`, `artifacts`. Every run is pinned to its commit, so anyone can replay it byte for byte.

## Get started

Have Trunks installed. Add a sandbox provider. Rename `.github/` to `.trunks/`. You're done.

The concepts below link to the full doc for each one in [docs/actions/](../../docs/actions/).

## Workflows

A YAML file under `.trunks/workflows/` triggered by `git push`. Imports the GitHub Actions schema. Supported: `name`, `on`, `jobs`, `needs`, `runs-on`, matrix, steps, `actions/checkout`, `actions/upload-artifact`, env, secrets, expressions like `${{ matrix.key }}` and `${{ github.sha }}`.

Anything else fails at lint time. See [docs/actions/workflows.md](../../docs/actions/workflows.md).

## Runs

A run is one command, executed once, against one commit, on one sandbox. Workflows produce runs. `trunks actions run` (one-shot), `trunks actions enqueue` + `trunks actions execute` (queue + claim), and the SDK produce runs.

Read results with `actions list`, `describe`, `logs`, `watch`, `cancel`. See [docs/actions/runs.md](../../docs/actions/runs.md).

## Sandbox providers

A provider boots a machine and runs the command. `local` and `docker` are built in. Hosted providers (`daytona`, `digitalocean`, `primeintellect`) are added with `--name` and a secret.

Provider secrets live in `~/.trunks/config`, not in the repo. The repo only needs workflow files and storage refs.

Every provider implements the same lifecycle: create, hydrate, upload, run, download, destroy. See [docs/actions/providers.md](../../docs/actions/providers.md).

## Specs

A `Spec` is the resource shape: CPU, memory, disk, arch, GPU, network. Default is `ubuntu-latest` (2/7/14). `runs-on:` picks a preset; `trunks.spec:` overrides per field.

| `runs-on` | CPU | Memory | Disk | GPU |
|---|---|---|---|---|
| `ubuntu-latest` | 2 | 7 GiB | 14 GiB | none |
| `ubuntu-large` / `*-4-cores` | 4 | 16 | 150 | none |
| `ubuntu-latest-8-cores` | 8 | 32 | 300 | none |
| `ubuntu-latest-16-cores` | 16 | 64 | 600 | none |
| `ubuntu-latest-32-cores` | 32 | 128 | 1200 | none |
| `gpu-h100` / `gpu-a100` / `gpu-l40s` | 8 | 32 | 100 | 1 |

See [docs/actions/specs.md](../../docs/actions/specs.md).

## Secrets

Trunks stores binding metadata, not values. `trunks actions secrets bind NPM_TOKEN env:NPM_TOKEN` reads `NPM_TOKEN` from the executor's environment at run time. Resolved values are masked in stored logs.

See [docs/actions/secrets.md](../../docs/actions/secrets.md).

## Capacity

A limit on how many runs of a given shape can be in flight at once. Set per `(region, spec_key)`. Slots are CAS-claimed before the run lease. Provider failures get recorded in health refs; soft routing prefers healthy providers.

See [docs/actions/capacity.md](../../docs/actions/capacity.md).

## Retention

Completed runs aren't deleted on the request path. `trunks actions prune --older-than 30d --keep-last 100` removes terminal refs. Object GC is a separate maintenance step (`trunks check --clean`).

See [docs/actions/retention.md](../../docs/actions/retention.md).

## Migration bridges

Three separate bridges that exist for repos moving off GitHub Actions:

**GitHub Checks writeback.** When Trunks finishes a run, it can post the green/red status back to the PR's Checks tab on GitHub. Useful when the team still uses GitHub for review but the actual CI runs on Trunks. Off by default; turn on by setting `TRUNKS_GITHUB_CHECKS_TOKEN` and `TRUNKS_GITHUB_REPOSITORY`.

**OIDC tokens.** Workflows that authenticate to AWS, GCP, etc. via `permissions.id-token: write` need a signing key. Trunks fails the workflow closed if the key isn't configured, so you don't get a silent no-op where a deploy step runs without credentials.

**Unsupported `uses:`.** Trunks natively runs `actions/checkout` and `actions/upload-artifact`. Anything else (e.g. `uses: actions/setup-node@v4`) fails at lint time. Override with `--accept-best-effort` to treat them as no-ops, but only when you've confirmed the action's behavior isn't load bearing.

See [docs/actions/migration-bridges.md](../../docs/actions/migration-bridges.md).

## Architecture

Branch refs and Actions refs are separate namespaces in Trunks storage. `git push` advances branch refs and triggers matching workflows. Queued runs, leases, logs, artifacts, capacity, and indexes live under `refs/actions/repos/<repo>/...`. Multiple executors against the same storage share one queue, fenced by CAS.

[Lifecycle diagram](../../docs/actions/lifecycle.mmd). [Storage refs diagram](../../docs/actions/storage-refs.mmd). [App integration refs](../../docs/actions/app-integration.md).

## For agent workloads

Trunks Actions is built for the workloads agents actually run.

**RL training and rollouts.** Spin up thousands of isolated environments in parallel. Each rollout is a run with its own commit, logs, and artifacts. The virtual filesystem means every rollout sees the full repo at zero hydration cost.

**Synthetic data generation.** Fan out a job over a matrix. Each cell writes its result as a content addressed artifact. Identical bytes dedupe across runs, so a hundred runs that produce the same intermediate file store it once.

**Evals and benchmarks.** TerminalBench style harnesses, coding evals, agent benchmarks. Every eval run is pinned to a commit OID and replayable byte for byte. The same eval can run on a laptop today and a Modal H100 next week and get bit identical inputs.

**Coding agents.** Claude Code, Codex, Devin and friends generate code that has to run somewhere that isn't the user's laptop. A `trunks actions run` from inside the agent's sandbox spins up a clean one. Trunks scoped run credentials mean the inner sandbox can read its commit closure and write only its own logs and artifacts. No broad cloud token, no cross run leakage.

**Fine tuning and inference jobs.** Request a GPU in the spec. The router picks a GPU capable provider, mounts the commit, runs the job, tears the sandbox down. No idle GPU fleet.

**Computer and browser use.** Per task sandboxes with full filesystem and network isolation. Each task is a run with replayable inputs.

Bring any GPU provider on board. Prime Intellect, Modal, Lambda, your own Firecracker fleet, your bare metal H100s. The router treats them as one pool. If one provider has a bad day, the next run goes to a healthy one without manual intervention.

## Trunks Actions vs GitHub Actions

| | GitHub Actions | Trunks Actions |
|---|---|---|
| Basic model | Keep runner machines ready for GitHub jobs. | Start compute for the job, store the result, shut it down. |
| Control plane | GitHub owns the queue, scheduler, runner assignment, logs, and status. | Trunks storage owns the queue, leases, logs, artifacts, and status. |
| GPUs | One SKU (T4) on hosted runners. For anything else (H100, A100, L40S, B200, RTX 5090) you bring the machine, register it as self-hosted, install drivers, patch the OS, and pay for idle time. | Request any GPU in the spec. Bring any provider on board: Prime Intellect, Modal, Lambda, your own bare metal pool. Trunks routes the run and tears the sandbox down when it ends. |
| Cost | Idle runners still cost money. Idle GPU runners cost a lot. | No idle runner fleet by default. Pay for the run, not the wait. |
| Failure boundary | Self-hosted runners still wait on GitHub Actions. | If one provider fails, route the job to a healthy one. |
| Agent triggers | Push, PR, schedule, webhook. The agent has to push to GitHub to run anything. | The agent calls `trunks actions run` from inside its own sandbox. No GitHub round-trip. |
