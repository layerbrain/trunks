# Agent Swarm Benchmarks

These benchmarks measure the scaling claim behind Trunks:

> Agent work should write to a user-owned storage backend directly, with commit/tree/object computation happening on each client machine, not on one hosted Git control plane.

The benchmark compares two local baselines:

- **Trunks**: many clients write commits to one shared Trunks store using content-addressed objects and CAS refs.
- **Git**: many clients clone, commit, and push to one local bare Git remote.

The Git baseline is intentionally local. GitHub would add network latency, auth, API limits, abuse controls, PR metadata, Actions, webhooks, and a multi-tenant control plane. This benchmark isolates repository-write mechanics.

## Run

```bash
python benchmarks/agent_swarm.py --mode all --scenario branches --changes 1000 --workers 64
```

JSON output:

```bash
python benchmarks/agent_swarm.py --mode all --scenario branches --changes 1000 --workers 64 --json
```

Use a fixed workdir if you want to inspect the stores:

```bash
python benchmarks/agent_swarm.py \
  --mode all \
  --scenario branches \
  --changes 1000 \
  --workers 64 \
  --workdir /tmp/trunks-agent-bench
```

## Scenarios

### Independent Branches

```bash
python benchmarks/agent_swarm.py --scenario branches --changes 1000 --workers 64
```

Each agent writes one file and pushes one branch:

```text
refs/heads/agent/000001
refs/heads/agent/000002
...
```

Expected Trunks behavior:

- All branches can succeed.
- Objects are immutable and can be written concurrently.
- Ref updates are independent CAS writes.
- No central Git server has to negotiate packs for every agent.

This is the normal agent-swarm shape.

### Same Branch Contention

```bash
python benchmarks/agent_swarm.py --scenario same-branch --changes 1000 --workers 64
```

Every agent races to push `main`.

Expected Trunks behavior:

- One writer wins.
- Stale writers fail with a ref conflict.
- The store is not corrupted.

This is not the recommended workflow. Agents should write isolated branches and merge after review.

## What To Look At

| Metric | Meaning |
|---|---|
| `successes` | Completed pushes |
| `failures` | Ref conflicts or process errors |
| `changes_per_second` | Successful pushed changes per wall-clock second |
| `p50_ms` / `p95_ms` | Per-agent elapsed time |
| `store_bytes` | Resulting remote size |

## Why Trunks Can Scale Differently Than GitHub

GitHub is a hosted, multi-tenant control plane. Every push goes through Git protocol handling, pack negotiation, auth, rate limits, hooks, PR metadata, and platform services.

Trunks moves the hot path to the client and the user's backend:

- Clients build blobs, trees, and commits locally.
- The backend stores immutable objects.
- Ref movement is the only coordinated operation.
- Independent agent branches update independent refs.
- GitHub stays on the review path instead of the high-frequency write path.

The benchmark does not claim Trunks is "bigger than GitHub." It shows why agent workloads should avoid making GitHub the high-frequency intermediate-state store.
