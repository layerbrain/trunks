# Actions Testing Matrix

This matrix describes the edge cases covered by the Actions and Sandboxes test suite. The goal is to prove the command surface, SDK surface, provider contract, and adversarial inputs behave predictably before provider integrations are added.

Run the focused suite with:

```bash
scripts/verify-actions.sh
```

Live hosted-provider checks are opt-in because they create provider resources:

```bash
DAYTONA_API_KEY=... scripts/verify-sandbox-provider.sh daytona --live
```

| Area | Edge case | Expected behavior | Coverage |
| --- | --- | --- | --- |
| CLI surface | Every `trunks actions` command and alias | Command returns the documented JSON shape or performs the requested mutation | `tests/actions/test_cli_surface.py` |
| CLI surface | Every `trunks sandboxes` provider command | Provider list/show/doctor/test/benchmark/specs/regions/scaffold work | `tests/actions/test_cli_surface.py` |
| SDK surface | Public Actions lifecycle functions | SDK can run, enqueue, cancel, list, load, repair, and execute work | `tests/actions/test_sdk_surface.py` |
| SDK surface | Public workflow functions | SDK can parse, lint, run, inspect, migrate, and read workflow logs | `tests/actions/test_sdk_surface.py` |
| SDK surface | Public sandbox registry and contract | SDK can discover, resolve, and validate providers | `tests/actions/test_sdk_surface.py` |
| RPC surface | Actions JSON-RPC methods | Daemon clients can enqueue, execute, list, get, cancel, and read health | `tests/actions/test_rpc_actions.py` |
| Sync/async API | Public calls outside an event loop | Calls run synchronously and return values | `tests/actions/test_run_lifecycle.py`, `tests/sandboxes/test_local_provider.py` |
| Sync/async API | Public calls inside an event loop | Calls return awaitables and execute asynchronously | `tests/actions/test_run_lifecycle.py`, `tests/actions/test_sdk_surface.py` |
| Repo state | Run inside a Trunks repo | State, logs, results, artifacts, and indexes are persisted to refs/objects | `tests/actions/test_run_lifecycle.py` |
| Repo state | Run outside a Trunks repo | Ad hoc run succeeds; repo-reading commands fail cleanly | `tests/actions/test_adversarial_edges.py` |
| Repo isolation | Multiple repos in one process | Executor only sees and mutates its own repo | `tests/actions/test_run_lifecycle.py` |
| Lifecycle boundary | Branch push starts `.trunks/workflows/*` with `on: push` | Git-compatible push behaves like CI/CD trigger, while Actions refs stay isolated under the Actions namespace | `tests/actions/test_lifecycle_boundaries.py`, `tests/unit/test_cli_gitshim.py` |
| Backend CAS | Concurrent writers on the same ref | Exactly one writer advances the ref | `tests/contract/backend.py` |
| Backend CAS | Concurrent writers on different refs | Independent refs advance without blocking each other | `tests/contract/backend.py` |
| Queueing | Two executors race for the same run | CAS lease allows exactly one executor | `tests/actions/test_run_lifecycle.py` |
| Queueing | Pending run asks for unsupported region/spec | Executor skips it and tries the next pending run | `tests/actions/test_adversarial_edges.py` |
| Queueing | Executor is configured with an unknown provider | Executor fails loudly instead of silently skipping forever | `tests/actions/test_adversarial_edges.py` |
| Fencing | Wrong token heartbeats/completions | Mutation is rejected | `tests/actions/test_run_lifecycle.py` |
| Fencing | Expired running lease | Claim increments attempt and issues a fresh token | `tests/actions/test_run_lifecycle.py` |
| Fencing | Long-running executor job | Executor refreshes the lease while the sandbox is still running | `tests/actions/test_run_lifecycle.py` |
| Fencing | Cancel while sandbox is running | Executor stops the sandbox and preserves the canceled state | `tests/actions/test_run_lifecycle.py` |
| Capacity | Capacity limit reached | Executor does not claim additional matching runs | `tests/actions/test_run_lifecycle.py` |
| Capacity | Negative capacity limit | API rejects invalid limit | `tests/actions/test_adversarial_edges.py` |
| Indexes | Missing derived index | Repair rebuilds it from primary run refs | `tests/actions/test_run_lifecycle.py` |
| Indexes | Corrupt/stale index ref | List/claim/repair skip the bad ref without crashing | `tests/actions/test_adversarial_edges.py` |
| Specs | CPU/memory/disk/gpu count are zero | Spec construction and CLI parsing reject it | `tests/actions/test_adversarial_edges.py` |
| Specs | Unknown arch/network | Spec construction rejects it | `tests/actions/test_adversarial_edges.py` |
| Runtime | Non-zero command exit | Run completes as failed with exit code | `tests/actions/test_adversarial_edges.py` |
| Runtime | Command timeout | Run completes as failed and records `timed_out` | `tests/actions/test_adversarial_edges.py` |
| Runtime | Local shell timeout with child process | Process group is killed so shell children do not keep running | `tests/sandboxes/test_local_provider.py` |
| Runtime | Large log stream | All log lines are preserved in order | `tests/actions/test_adversarial_edges.py` |
| Runtime | Logs while process is still running | Provider streams the first log before process completion | `tests/sandboxes/test_local_provider.py` |
| Runtime | Watch while executor job is running | Logs are persisted incrementally and watch sees them before completion | `tests/actions/test_run_lifecycle.py` |
| Runtime | Process exits during timeout/cancel race | Cancellation is idempotent and does not raise `ProcessLookupError` | `tests/actions/test_adversarial_edges.py` |
| Hydration | Remote worktree upload | Hosted providers receive trackable workspace files under `/workspace` | `tests/actions/test_remote_hydration.py` |
| Hydration | Commit-pinned run | Executor materializes the requested Trunks commit instead of current worktree state | `tests/actions/test_remote_hydration.py` |
| Workflows | `.trunks/workflows/*.yml` | Workflow parse/run/list/show/lint works | `tests/actions/test_workflow_yml.py`, `tests/actions/test_cli_surface.py` |
| Workflows | `on:` YAML key | Parsed as a trigger key, not YAML boolean | `tests/actions/test_workflow_yml.py` |
| Workflows | `needs` DAG | Jobs run in dependency order | `tests/actions/test_workflow_yml.py` |
| Workflows | `needs` cycle or missing dependency | Parse fails closed | `tests/actions/test_adversarial_edges.py` |
| Workflows | Matrix fanout | Matrix jobs expand and run | `tests/actions/test_workflow_yml.py` |
| Workflows | Matrix explosion | Parse fails above the bounded v1 limit | `tests/actions/test_adversarial_edges.py` |
| Workflows | Invalid YAML | Lint reports invalid YAML without crashing | `tests/actions/test_adversarial_edges.py` |
| Workflows | Invalid env variable name | Parse fails before shell execution | `tests/actions/test_adversarial_edges.py` |
| Workflows | Invalid `trunks.spec` | Parse fails before routing | `tests/actions/test_adversarial_edges.py` |
| Workflows | Unsupported marketplace action | Strict mode fails closed; best-effort mode accepts | `tests/actions/test_workflow_yml.py` |
| Workflows | `environment:` | Strict mode fails closed | `tests/actions/test_workflow_yml.py` |
| Workflows | `permissions.id-token: write` without bridge | Strict mode fails closed | `tests/actions/test_workflow_yml.py` |
| Migration | `.github/workflows/*.yml` import | File can be copied into `.trunks/workflows` | `tests/actions/test_workflow_yml.py`, `tests/actions/test_cli_surface.py` |
| Secrets | Secret binding metadata | Trunks stores source binding, not plaintext value | `tests/actions/test_run_lifecycle.py`, `tests/actions/test_sdk_surface.py` |
| Secrets | Invalid secret name | Binding fails closed | `tests/actions/test_adversarial_edges.py` |
| Artifacts | File and directory artifacts | Artifacts store as Trunks blobs and can be listed/downloaded | `tests/actions/test_run_lifecycle.py`, `tests/actions/test_sdk_surface.py` |
| Artifacts | `../` artifact path escape | Collection is rejected | `tests/actions/test_adversarial_edges.py` |
| Artifacts | Symlink artifact escape | Collection is rejected | `tests/actions/test_adversarial_edges.py` |
| Artifacts | macOS `/var` to `/private/var` temp paths | Artifact hierarchy remains stable after path resolution | `tests/actions/test_adversarial_edges.py` |
| Retention | Dry-run prune | Reports terminal runs without deleting refs | `tests/actions/test_run_lifecycle.py`, `tests/actions/test_cli_surface.py` |
| Retention | Active run protection | Prune skips pending and running work | `tests/actions/test_run_lifecycle.py` |
| Retention | Object cleanup separation | Ref pruning is separate from expensive object GC | `docs/actions.md` |
| Providers | Local provider contract | Hydrate/upload/run/log/download/destroy passes | `tests/sandboxes/test_local_provider.py` |
| Providers | Docker provider contract | Optional Docker provider passes the same contract when daemon is available | `tests/sandboxes/test_local_provider.py` |
| Providers | Docker CLI action | Container run executes through `--provider docker --strict-provider --isolation container` | `tests/sandboxes/test_local_provider.py` |
| Providers | Hosted Python provider mapping | Daytona provider calls the official REST API for create/execute/upload/download/delete without provider SDK code | `tests/sandboxes/test_daytona_provider.py` |
| Providers | Hosted orphan cleanup | Hosted provider resources can be listed and cleaned by prefix through the provider class | `tests/sandboxes/test_daytona_provider.py` |
| Providers | VM-backed compute provider | DigitalOcean uses REST for Droplet/SSH-key lifecycle and SSH for run/upload/download without provider SDK code | `tests/sandboxes/test_digitalocean_provider.py` |
| Providers | VM cleanup on create failure | DigitalOcean deletes created SSH keys if Droplet creation fails | `tests/sandboxes/test_digitalocean_provider.py` |
| Providers | VM secret injection | DigitalOcean sends environment values over SSH stdin instead of local SSH argv | `tests/sandboxes/test_digitalocean_provider.py` |
| Providers | Provider health routing | Recent provider infrastructure failures route soft claims to healthy matching providers first | `tests/actions/test_provider_health.py` |
| Providers | Provider soft failure | Router falls back to the next matching provider | `tests/sandboxes/test_local_provider.py` |
| Providers | Docker sandbox path escape | Upload/download paths cannot escape the container temp root | `tests/sandboxes/test_adversarial_sandboxes.py` |
| Providers | Docker env injection | Secret values are not placed in `docker run` argv | `tests/sandboxes/test_adversarial_sandboxes.py` |
| GitHub writeback | Checks API payload | Pure HTTP writeback sends expected GitHub Checks request | `tests/actions/test_status_writeback.py` |
| GitHub writeback | Explicit env-gated writeback | Completed runs post Checks only when token and repository env are configured | `tests/actions/test_status_writeback.py` |
| OIDC bridge | Missing signing key | Token minting fails closed | `tests/actions/test_oidc_bridge.py` |
| OIDC bridge | Signed token claims | Short-lived token includes run/repo/provider claims and rejects tampering | `tests/actions/test_oidc_bridge.py` |

Live hosted-provider execution remains opt-in because it creates real external compute. Add hosted providers only after verifying their official API exposes the sandbox lifecycle; use SSH-backed execution when the provider provisions a VM or pod instead of exposing an HTTP exec endpoint.
