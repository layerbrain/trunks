# Lifecycle

Five verbs. They're all you need.

```text
mount  →  edit  →  checkpoint  →  push
                                     ↓
                                  pull (anywhere else)
```

## 1. Mount

```bash
trunks mount --repo my-app --path ./my-app
```

Trunks opens a workspace at `./my-app` for the repo `my-app`.

```text
backend                        local
─────────                      ─────
refs/heads/main ──► commit ──► tree ──► files in ./my-app
```

## 2. Edit

Plain files. Plain tools.

```bash
echo "Fix auth" > ./my-app/task.md
npm test --prefix ./my-app
```

## 3. Checkpoint

```bash
cd ./my-app
trunks checkpoint -m "update auth"
```

A checkpoint is a real Git commit:

```text
blob 9f86...      file contents
tree 5d41...      path → blob mapping
commit 78ab...    parent + tree + message
refs/heads/main → 78ab...
```

Checkpoint is local. Nothing has gone over the network yet.

## 4. Push

```bash
git push
```

When the Trunks git shim handles `git push`, it uploads missing objects and advances the backend ref with compare-and-swap.

```text
local objects → backend objects   (one PUT per missing blob/tree/commit)
local ref     → backend ref       (one CAS, atomic)
```

If another writer moved the same branch first, the CAS fails and your push errors out cleanly. Pull, rebase or merge, push again.

## Multi-Host Safety

Trunks never advances a backend branch by blind overwrite. Every backend implements the same ref CAS contract:

- many writers racing one ref produce one winner
- writers updating different refs can all succeed
- objects are content-addressed and verified before storage accepts them
- mirror failures in strict mode do not advance the primary ref

The contract runs against memory, local disk, fileshare, SQLite, S3-compatible storage, Postgres, GCS, Azure Blob, and SFTP. Object-store and SFTP backends use lock objects for the short ref-update section; SQL backends use database transactions or advisory transaction locks.

Actions run refs are a separate namespace in the same storage. A `git push` starts matching `.trunks/workflows/*` files with `on: push`; queued runs, leases, logs, artifacts, capacity, and indexes are CAS-fenced under `refs/actions/repos/<repo>/...`.

## 5. Pull

```bash
trunks pull
```

Pull reads backend refs, downloads any missing objects, and updates your workspace.

## Branches

A branch is a pointer. Creating one is one ref write. There is no copy.

```bash
trunks branch create --name feature/auth --from main
trunks branch switch --name feature/auth
```

```text
refs/heads/main      → commit-A
refs/heads/feature/auth → commit-A   (same commit, different name)
```

The agent commits, push moves only `refs/heads/feature/auth`. `main` doesn't budge.

## What Goes Wrong, And What To Do

| Symptom | Likely cause | Fix |
|---|---|---|
| `push` errors with a CAS failure | Another writer pushed first | `git pull`, resolve, `git push` |
| `mount` errors that the repo doesn't exist | No `repo create` for that name | `trunks repo create --name <name> --backend <url>` |
| Files missing after `pull` | Stale ref cache | `trunks fetch && trunks pull` |
| Pushed bytes but the other machine sees nothing | Different storage root in config | Check `trunks repo get --name <name> --json` |
