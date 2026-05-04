# Sandbox Providers

Sandbox providers are compute backends that satisfy the Trunks sandbox protocol. Actions uses providers to create isolated sessions, hydrate files, run commands, stream logs, move artifacts, cancel work, and destroy the session.

## Inspect Providers

```bash
trunks sandboxes providers --json
trunks sandboxes providers show --name local --json
trunks sandboxes specs --provider local
trunks sandboxes regions --provider local
```

## Validate Providers

```bash
trunks sandboxes providers doctor --name local --json
trunks sandboxes providers test --name local --json
trunks sandboxes providers benchmark --name local --json
trunks sandboxes providers orphans --name daytona --json
trunks sandboxes providers cleanup --name daytona --dry-run --json
scripts/verify-sandbox-provider.sh --name daytona
```

`doctor` inspects registered capability metadata. `test` runs the provider contract. `benchmark` runs the same contract and records elapsed time. Remote providers require `--live` for `test` and `benchmark` because those commands create real provider resources.
`orphans` lists provider resources whose names match the Trunks sandbox prefix. `cleanup` deletes those resources only when `--dry-run` is omitted.
`scripts/verify-sandbox-provider.sh --name <provider>` checks provider discovery and metadata without creating a sandbox. Add `--live` to run the provider contract and create real provider resources:

```bash
scripts/verify-sandbox-provider.sh --name daytona --live
```

## Provider Rules

- Providers must be async-first.
- First-party providers are Python classes under `trunks/sandboxes/providers/*.py`.
- Do not add provider SDK dependencies to Trunks.
- Providers must advertise only capabilities they can enforce.
- Provider specs are bundled in the provider class and exported for connected providers by `trunks sandboxes specs --json`; routing does not poll provider catalogs on the hot path.
- VM-backed providers can run CI/CD over general compute, but they should advertise VM isolation and measured startup honestly.

The built-in `local` provider is for trusted development. It runs a host process and only advertises default networking because it cannot enforce network isolation.

The built-in `docker` provider is discovered when Docker is installed and the daemon is reachable. It runs commands inside Linux containers through the Docker CLI, without adding a Docker SDK dependency.

```bash
trunks sandboxes providers test --name docker --json
trunks actions run --provider docker --strict-provider --isolation container --command "echo ok" --json
```

The built-in `daytona` provider calls Daytona's REST API directly; no Daytona SDK package is installed.

```bash
trunks sandboxes providers add \
  --name daytona \
  --type daytona \
  --secret api_key=...
trunks sandboxes providers show --name daytona --json
trunks sandboxes providers test --name daytona --live --json
trunks actions run --provider daytona --strict-provider --isolation container --command "echo ok" --json
trunks sandboxes providers orphans --name daytona --json
```

The built-in `digitalocean` provider creates a temporary SSH key, boots a tagged Droplet, runs commands as `root` over SSH under `/root/trunks`, then deletes the Droplet and SSH key. It uses no DigitalOcean SDK package.

```bash
trunks sandboxes providers add \
  --name digitalocean \
  --type digitalocean \
  --secret api_key=... \
  --set region=nyc3
trunks sandboxes providers show --name digitalocean --json
trunks sandboxes providers test --name digitalocean --live --json
trunks actions run --provider digitalocean --strict-provider --isolation vm --arch x86_64 --command "echo ok" --json
trunks sandboxes providers orphans --name digitalocean --json
trunks sandboxes providers cleanup --name digitalocean --dry-run --json
```

The built-in `primeintellect` provider uses Prime Intellect's REST API for SSH-key and pod lifecycle, then SSH for run/upload/download. Its bundled catalog advertises GPU specs such as H100, H200, A100, B200, L40S, RTX 4090, and RTX 5090 classes.

```bash
trunks sandboxes providers add \
  --name primeintellect \
  --type primeintellect \
  --secret api_key=...
trunks sandboxes providers show --name primeintellect --json
trunks sandboxes providers test --name primeintellect --live --json
trunks actions run --provider primeintellect --strict-provider --isolation vm --gpu H100_80GB --gpu-count 1 --command "nvidia-smi" --json
trunks sandboxes providers orphans --name primeintellect --json
trunks sandboxes providers cleanup --name primeintellect --dry-run --json
```

Provider profiles are stored in `~/.trunks/config` and masked in CLI output. API keys are local machine credentials, not repo metadata. Environment variables such as `TRUNKS_DAYTONA_API_KEY`, `TRUNKS_DIGITALOCEAN_API_TOKEN`, and `TRUNKS_PRIMEINTELLECT_API_KEY` are still accepted for ephemeral testing or CI overrides.

Unavailable optional providers are skipped during discovery when their `discover(...)` method raises `LookupError`. Selecting a missing provider explicitly with `--provider <id> --strict-provider` still fails loudly.

Injected environment values are passed without embedding secret values in the `docker run` argument list. Docker still receives them as container environment variables, so use a trusted Docker host for secret-bearing jobs.

Hosted providers receive a workspace through the sandbox upload protocol. `worktree` uploads the current trackable files, and commit-pinned runs materialize the requested Trunks commit before upload. A hosted provider should not be marked production-ready until its live contract and cleanup flow prove that create, upload, execute, download, delete, and orphan listing all work against the real provider API.
