# Actions

Trunks Actions lets agents run CI/CD at scale with GitHub-compatible workflows, fast-starting compute, provider failover, GPU-ready jobs, and replayable results without depending on GitHub Actions.

The short walkthrough lives in [trunks/actions/README.md](../trunks/actions/README.md).

## Concepts

- [Workflows](actions/workflows.md) — YAML under `.trunks/workflows/`, GitHub-compatible subset
- [Runs](actions/runs.md) — one job, one command, one commit, one sandbox
- [Sandbox providers](actions/providers.md) — local, Docker, Daytona, DigitalOcean, Prime Intellect
- [Specs](actions/specs.md) — CPU, memory, disk, GPU, network; how `runs-on` maps
- [Secrets](actions/secrets.md) — bindings, masking, naming rules
- [Capacity](actions/capacity.md) — slot limits per `(region, spec_key)`, provider health
- [Retention](actions/retention.md) — prune, object GC, scheduling

## Bridges

- [Migration bridges](actions/migration-bridges.md) — GitHub Checks writeback, OIDC, unsupported `uses:`
- [App integration](actions/app-integration.md) — refs schema for app builders
- [Examples](actions/examples.md) — minimal headless CI runner

## Architecture

- [Lifecycle diagram](actions/lifecycle.mmd)
- [Storage refs diagram](actions/storage-refs.mmd)
- [Testing matrix](actions/testing-matrix.md)
