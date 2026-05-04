# Trunks

The most powerful open-source POSIX-compatible, Git-native filesystem.

**Trunks turns any backend into a Git-compatible remote.** Point it at S3, R2, GCS, Azure Blob, Postgres, SFTP, a fileshare, or local disk. You get branches, commits, refs, push, pull, the whole protocol. No Git server. No service to operate. No control plane. No repo copy per workspace.

Applications write normal files. Developers run normal Git.

```bash
pip install trunks
npm install @layerbrain/trunks

mkdir my-app && cd my-app
git init --initial-branch=main
trunks init --name my-app
trunks storage add primary --backend s3 --bucket company-trunks
git remote add origin trunks://primary/my-app
echo "Fix auth" > task.md
git add .
git commit -m "update auth"
git push -u origin main
```

[Get started](tutorial.md){ .md-button .md-button--primary }
[Lifecycle](lifecycle.md){ .md-button }

## How It Fits Together

```text
   ┌─────────────────────────────────────────────────┐
   │ CLI · Python SDK · Node SDK · Git shell · mount │
   └────────────────────┬────────────────────────────┘
                        │
                        ▼
                 local Trunks repo
                        │
                        ▼
                 your configured backend
                 (objects · refs · journals)
```

## Core Ideas

| Concept | Meaning |
|---|---|
| Repo name | Stable identity. `my-app`. |
| Storage profile | Named backend config. `primary`. |
| Storage root | Where bytes live. `s3://company-trunks`. |
| Derived trunk | `s3://company-trunks/trunks/my-app.trunk`. |
| Checkpoint | A real Git commit object. |
| Branch | A ref pointer. Use one per task. |
| CAS | Compare-and-swap on ref updates. Two writers can't clobber. |
| Remote helper | `git-remote-trunks`, invoked by Git for `trunks://...` remotes. |

## Docs

| Need | Page |
|---|---|
| First setup | [Tutorial](tutorial.md) |
| Mount, edit, checkpoint, push, pull | [Lifecycle](lifecycle.md) |
| Every command | [CLI reference](cli.md) |
| Python | [Python SDK](sdk-python.md) |
| Node | [Node SDK](sdk-node.md) |
| Actions and sandbox providers | [Actions](actions.md) |
| Resource shapes | [Resources](resources.md) |
| Storage setup | [Backends](backends/README.md) |
| Agent framework recipes | [Agents](agents.md) |
| Internals | [Architecture](architecture.md) |

MIT licensed.
