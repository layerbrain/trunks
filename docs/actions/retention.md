# Retention

Trunks doesn't delete completed runs on the request path. Pruning is a separate maintenance command.

## Prune

```bash
trunks actions prune --older-than 30d --keep-last 100 --dry-run --json
trunks actions prune --older-than 30d --keep-last 100 --json
```

`--older-than` is a duration (`30d`, `7d`, `12h`). `--keep-last N` always keeps the N most recent runs even if they're older than the cutoff. Both apply together: a run is deleted only if it's older than the cutoff AND not in the keep-last window.

`--dry-run` reports what would be deleted without touching anything.

Prune only removes runs in terminal states (`succeeded`, `failed`, `canceled`, `timed_out`). Pending and running work is never deleted.

## Objects vs refs

Pruning a run removes its refs: state, lease, logs, artifacts index, attempt history. The underlying content-addressed objects — log chunks, artifact blobs — stay until object GC runs.

```bash
trunks actions prune --older-than 30d --clean-objects --json
trunks check --clean
```

`--clean-objects` runs object GC inline. `trunks check --clean` runs it as a separate operation. Object GC walks every object to find unreachable ones; it's more expensive than ref deletion.

## Maintenance lease

```bash
trunks actions prune --lease-ttl 300 --json
trunks actions prune --force --json
```

Prune CAS-claims a maintenance lease before deleting anything. Default TTL is 300 seconds. `--force` breaks a stale lease.

## Scheduling

Run prune from a scheduled maintenance executor or cron. A reasonable default: prune daily, GC weekly.

## What doesn't get pruned

- Pending and running work.
- Capacity limits and provider health refs.
- Secret bindings.
- Workflow definitions in `.trunks/workflows/`.
