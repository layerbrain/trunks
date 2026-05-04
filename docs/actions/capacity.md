# Capacity

Capacity is how many runs of a given shape can be in flight at once. It's a limit, not a queue. When the limit is hit, new claims wait; pending runs stay in the queue until a slot frees up.

## Setting a limit

```bash
trunks actions capacity set local cpu2-mem7g-disk14g-arm64-nogpu-default max-concurrent 4 --json
trunks actions capacity --json
```

The key is `(region, spec_key)`. Two runs with the same `Spec` share the same key. Two runs with different specs don't compete for the same slot.

`spec_key` comes from the canonical hash of the resource shape. It's reported by `actions describe` on every run.

## How slots work

Each `(pool, region, spec_key)` has a numbered set of slot refs: `slot/0`, `slot/1`, ..., up to `max_concurrent - 1`. An executor CAS-claims a free slot before claiming the run lease. Both must succeed.

A slot has a TTL. If the executor dies without releasing it, another executor reclaims the slot once the TTL expires.

When `max_concurrent` is unset, capacity is unlimited for that key. The executor still claims a sentinel record so `capacity --json` can show what's running, but nothing blocks.

## Health

```bash
trunks actions health --json
trunks actions index repair --json
```

When a provider fails to boot a sandbox or loses heartbeat, the failure is recorded under provider health refs. Soft routing prefers healthy matching providers. A provider whose lapse rate is above the configured floor is taken out of rotation entirely until an operator resets it.

`index repair` rebuilds derived index refs (`list`, `workflow-runs`, capacity snapshot) from the underlying run refs. Run it after a backend restore or if you suspect indexes drifted.

Capacity bounds concurrency per shape, not per workflow. It does not bound spend, enforce fairness across repos, or autoscale.
