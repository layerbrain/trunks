# Workflows

A workflow is a YAML file under `.trunks/workflows/`. It looks like a GitHub Actions workflow because that's what it imports from. The trigger is `git push`. The runner is a sandbox the executor picks at run time.

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

Trunks-managed `git push` publishes the commit closure to storage and starts every workflow whose `on:` matches. Remote-helper pushes to `trunks://...` write durable push trigger refs in storage; daemon pickup for those trigger refs is the storage-side Actions path.

## What's supported

- `name`
- `on` — `push`, `workflow_dispatch`, list form, mapping form
- `jobs.<id>` — one job per id
- `jobs.<id>.needs` — string or list, must reference real jobs
- `jobs.<id>.runs-on` — resolves to a `Spec`; see [specs.md](specs.md)
- `jobs.<id>.strategy.matrix` — bounded fanout
- `jobs.<id>.steps[].run` — bash, runs in the sandbox
- `jobs.<id>.steps[].uses: actions/checkout@v*`
- `jobs.<id>.steps[].uses: actions/upload-artifact@v*`
- `jobs.<id>.steps[].env` — per-step environment
- `${{ matrix.key }}`
- `${{ github.sha }}`, `${{ github.run_id }}`, `${{ github.workflow }}`, `${{ github.job }}`
- `${{ secrets.NAME }}` — see [secrets.md](secrets.md)

Anything else fails at lint time.

## What fails closed

- `environment:` on a job
- `permissions.id-token: write` without the OIDC bridge configured
- `uses:` actions other than `actions/checkout` and `actions/upload-artifact`
- Matrix expansions over 256 cells
- `needs` cycles or references to unknown jobs
- Invalid environment variable names
- Invalid `trunks.spec`

Override unsupported `uses:` with `--accept-best-effort` to make Trunks treat them as no-ops.

## Migrating from GitHub Actions

```bash
trunks actions migrate .github/workflows/ci.yml
trunks actions workflows lint --json
git add .trunks/workflows
git commit -m "Move CI to Trunks"
git push
```

`migrate` copies the file. `lint` reports what won't run.

## Listing and inspecting

```bash
trunks actions workflows --json
trunks actions workflows lint --json
trunks actions workflows show <name> --json
trunks actions workflow-runs --json
trunks actions workflow-runs --id <id> jobs --json
trunks actions workflow-runs --id <id> graph --json
trunks actions workflow-runs --id <id> logs --json
```

`workflow-runs` is the per-workflow view. `actions list` is the per-job view. They link by run id.

## Triggers

`on: push` fires when Trunks advances a branch ref. `workflow_dispatch` runs the workflow on demand:

```bash
trunks actions run <workflow-name> --json
```

Trigger sources: post-commit, Trunks-managed `git push`, remote-helper push trigger refs, CLI, SDK. External forge webhooks (GitHub, GitLab) are not yet supported.
