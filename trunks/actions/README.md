# Trunks Actions

Have Trunks installed. Add a sandbox provider. Rename `.github/` to `.trunks/`. You're done.

The full command and API reference lives in [docs/actions.md](../../docs/actions.md).

## 1. Add compute

```bash
trunks sandboxes providers add daytona-main \
  --type daytona \
  --secret api_key=...
```

Swap `daytona` for any provider Trunks knows about (`local`, `docker`, `digitalocean`, `primeintellect`, ...). The local process provider is always available without any setup.

## 2. Move your workflow

```bash
trunks actions migrate .github/workflows/ci.yml
```

Or write one directly under `.trunks/workflows/`:

```yaml
name: CI
on: [push, workflow_dispatch]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3.12 -m unittest
```

The `runs-on` label resolves to a Trunks `Spec` automatically. Override per field with `trunks.spec` when you need more.

## 3. Push

```bash
git add .
git commit -m "Add Trunks CI"
git push

trunks actions workflow-runs --json
```

`git push` through the Trunks shim publishes the commit closure to storage and starts every matching `on: push` workflow. No GitHub Actions control plane is required.

## Defaults

A job with no `runs-on` and no `trunks.spec` runs with the same shape as a GitHub-hosted `ubuntu-latest` runner.

| `runs-on` label | CPU | Memory (GiB) | Disk (GiB) | GPU |
|---|---|---|---|---|
| _(unset)_ / `ubuntu-latest` / `ubuntu-24.04` / `ubuntu-22.04` | 2 | 7 | 14 | — |
| `ubuntu-large` / `ubuntu-latest-4-cores` | 4 | 16 | 150 | — |
| `ubuntu-latest-8-cores` | 8 | 32 | 300 | — |
| `ubuntu-latest-16-cores` | 16 | 64 | 600 | — |
| `ubuntu-latest-32-cores` | 32 | 128 | 1200 | — |
| `gpu-h100` | 8 | 32 | 100 | 1 × H100 80GB |
| `gpu-a100` | 8 | 32 | 100 | 1 × A100 80GB |
| `gpu-l40s` | 8 | 32 | 100 | 1 × L40S 48GB |

Unknown labels fall back to the `ubuntu-latest` shape. Any field on `trunks.spec` overrides the preset.

## One-off runs

```bash
trunks actions run --command "python3.12 -m unittest" --json
```

```bash
trunks actions enqueue --command "python3.12 -m unittest" --json
trunks actions execute --id executor-1 --json
```

## Reading results

```bash
trunks actions list --json
trunks actions describe --id <run-id> --json
trunks actions logs --id <run-id>
trunks actions artifacts --id <run-id> --json
trunks actions workflow-runs --json
```

## Why this works without GitHub

GitHub Actions keeps runners waiting for jobs. Trunks Actions starts compute when work exists. The queue, leases, logs, artifacts, capacity, and health refs all live in your Trunks storage. Any executor pointed at the same storage participates in the same queue. If one provider fails, the executor routes to a healthy one.

For the full lifecycle, sandbox provider matrix, secrets, capacity, OIDC, GitHub Checks writeback, and adversarial guarantees, read [docs/actions.md](../../docs/actions.md).
