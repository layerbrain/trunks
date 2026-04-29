# Trunks

The most powerful open-source POSIX-compatible, Git-native filesystem for AI agents.

**Trunks turns any backend into a Git-compatible remote.** Point it at S3, R2, GCS, Azure Blob, Postgres, SFTP, a fileshare, or local disk. You get branches, commits, refs, push, pull, the whole protocol. No Git server. No service to operate. No control plane. No repo copy per agent.

Agents write normal files. Developers run normal Git.

```bash
pip install trunks
npm install @layerbrain/trunks

trunks repo create --name my-app --backend s3://company-trunks
trunks mount --repo my-app --path ./my-app
echo "Fix auth" > my-app/task.md
trunks checkpoint -m "agent output"
trunks push
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
| Storage root | Where bytes live. `s3://company-trunks`. |
| Derived trunk | `s3://company-trunks/trunks/my-app.trunk`. |
| Checkpoint | A real Git commit object. |
| Branch | A ref pointer. Use one per agent run. |
| CAS | Compare-and-swap on ref updates. Two writers can't clobber. |

## Docs

| Need | Page |
|---|---|
| First setup | [Tutorial](tutorial.md) |
| Mount, edit, checkpoint, push, pull | [Lifecycle](lifecycle.md) |
| Every command | [CLI reference](cli.md) |
| Python | [Python SDK](sdk-python.md) |
| Node | [Node SDK](sdk-node.md) |
| Resource shapes | [Resources](resources.md) |
| Storage setup | [Backends](backends/README.md) |
| Agent framework recipes | [Agents](agents.md) |
| Internals | [Architecture](architecture.md) |

MIT licensed.
