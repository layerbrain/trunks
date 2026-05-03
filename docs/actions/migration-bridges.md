# Migration Bridges

Trunks Actions can run CI while another forge still displays review state.

## GitHub Checks Writeback

If a repo is still reviewed on GitHub, Trunks can post completed check runs to the GitHub Checks API. GitHub displays the green or red check; Trunks remains the source of truth for execution, logs, artifacts, and run history.

The writeback implementation is pure HTTP and uses the GitHub Checks API. It does not run GitHub Actions.

Required inputs:

- GitHub App or token with Checks write permission
- owner
- repo
- commit SHA
- Trunks run id
- conclusion

The check payload uses the Trunks run id as `external_id` so retries are traceable.

Executor writeback is disabled unless these variables are present:

```bash
export TRUNKS_GITHUB_CHECKS_TOKEN=...
export TRUNKS_GITHUB_REPOSITORY=owner/repo
export TRUNKS_ACTIONS_DETAILS_URL="https://trunks.example/runs/{run}"
```

`GITHUB_TOKEN` and `GITHUB_REPOSITORY` are accepted for CI environments that already provide them.

## Trunks OIDC Token Bridge

Workflows that request `permissions.id-token: write` fail closed unless OIDC is explicitly enabled. The v1 bridge mints a short-lived signed token for a completed or running Trunks action run:

```bash
export TRUNKS_OIDC_SIGNING_KEY=...
trunks actions oidc token --id <run-id> --audience aws --json
```

The token contains the repository, run id, commit, provider, region, attempt, issuer, audience, issue time, and expiry. Cloud-specific trust exchange is configured outside Trunks by the operator; Trunks does not mint long-lived cloud credentials.

## Unsupported Marketplace Actions

Unsupported `uses:` entries fail at lint time by default. This prevents a workflow from silently running with different semantics after migration.

Currently supported shims:

- `actions/checkout`
- `actions/upload-artifact`

Use `--accept-best-effort` only when the migration explicitly accepts best-effort behavior.
