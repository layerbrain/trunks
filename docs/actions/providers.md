# Sandbox Providers

A sandbox provider runs the job. The executor picks one based on what the `Spec` asks for and what each provider can satisfy.

## Built in

- `local` — runs a local process. No isolation. Always available.
- `docker` — runs a container. Discovered automatically when Docker is installed and the daemon is reachable.

```bash
trunks sandboxes providers --json
trunks sandboxes providers test --name local
trunks sandboxes providers test --name docker
```

## Hosted

Hosted providers are connected with a name and a secret:

```bash
trunks sandboxes providers add \
  --name daytona \
  --type daytona \
  --secret api_key=...
trunks sandboxes providers test --name daytona --live --json
```

The profile is written to `~/.trunks/config`. Provider API keys are local executor credentials, not committed repo state.

Implemented hosted providers: Daytona, DigitalOcean, Prime Intellect. Daytona is HTTP-exec. DigitalOcean is VM-backed over SSH. Prime Intellect is GPU pods over SSH.

Hosted providers receive a workspace through the sandbox upload protocol. `worktree` runs upload trackable files. Commit-pinned runs materialize the requested commit before upload.

```bash
trunks sandboxes providers add \
  --name digitalocean \
  --type digitalocean \
  --secret api_key=... \
  --set region=nyc3
trunks actions run --provider digitalocean --strict-provider --isolation vm --arch x86_64 --command "echo ok" --json

trunks sandboxes providers add \
  --name primeintellect \
  --type primeintellect \
  --secret api_key=...
trunks actions run --provider primeintellect --strict-provider --isolation vm --gpu H100_80GB --gpu-count 1 --command "nvidia-smi" --json
```

## Contract test

```bash
trunks sandboxes providers test --name <provider-name>
```

Exercises create, hydrate, upload, run, download, destroy.

Hosted providers also support orphan listing and cleanup:

```bash
trunks sandboxes providers orphans --name daytona --json
trunks sandboxes providers cleanup --name daytona --dry-run --json
```

## Failover

When a provider fails to boot a sandbox or loses heartbeat, the failure is recorded under provider health refs. The router prefers healthy matching providers. A provider whose lapse rate is above the configured floor is taken out of rotation until reset.

```bash
trunks actions health --json
```
