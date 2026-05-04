# Trunks

The most powerful open-source POSIX-compatible, Git-native filesystem.

**Trunks turns any backend into a Git-compatible remote.** Point it at S3, R2, Tigris, GCS, Azure Blob, MinIO, Postgres, SFTP, a fileshare, or local disk. You get branches, commits, refs, push, pull, the whole protocol. No Git server. No service to operate. No control plane. No repo copy per workspace.

Applications write normal files. Developers run normal Git.

```bash
git remote add origin trunks://primary/my-app
git push -u origin main
```

That `git push` invokes `git-remote-trunks`, writes commit objects, and advances the branch ref straight into your configured storage. No GitHub. No Git server in between.

## Install

```bash
pip install trunks
npm install @layerbrain/trunks
```

## Quick Start

```bash
trunks storage add --name primary --backend local --path /tmp/trunks-store
mkdir my-app
cd my-app
trunks mount --repo my-app
echo "hello" > README.md
git add README.md
git commit -m "init"
git push -u origin main
```

That's it. `trunks mount` initializes `.git` and wires `origin -> trunks://primary/my-app` for you. Another machine can run `git clone trunks://primary/my-app`.

S3 here is whatever you have: GCS, Azure Blob, R2, Tigris, MinIO, Postgres, SFTP, fileshare, local disk. Trunks makes each one act like a Git remote. No Trunks server in the middle. No Git server in the middle.

## How It Works

Trunks stores repos as Git-shaped objects in your backend. Real Git owns `.git`; Trunks only participates when Git invokes `git-remote-trunks` for a `trunks://` remote.

```text
s3://company-trunks/trunks/my-app.trunk/
├── objects/       blobs, trees, commits (sha-keyed, immutable)
├── refs/heads/    branch pointers (compare-and-swap)
└── journals/      crash recovery
```

- When a process writes a file, Trunks hashes the bytes into objects.
- When Git pushes, the remote helper uploads missing objects.
- When the push completes, Trunks advances the branch ref with compare-and-swap.

That's the whole protocol. Two writers can't clobber each other. Crashes don't corrupt state. Two machines sync by pointing at the same prefix.

## Why It Exists

Modern software moves a lot of files across laptops, CI, sandboxes, servers, and storage buckets. Without version history, every write is just another blob that can overwrite the last one.

Git solves history. But Git expects a hosted server, a clone per agent, and has no native story for large or many repos.

Trunks keeps Git's commit objects and refs, drops the server, and writes straight to storage you already use. The bucket is the remote. So is the database, or the SFTP host. Work happens on branches, files are saved as commits, and diffs stay reviewable like any normal Git workflow.

## What You Get

- **Real files.** Tools read and write with `cat`, `vim`, `grep`, `npm`, `python`. The SDKs are a convenience, not a requirement.
- **Real Git.** `git log`, `git diff`, `git blame`, `git push`, and `git clone trunks://...` operate through Git's standard remote-helper protocol.
- **Real concurrency.** Branches are pointers. Different branches never collide. Same branch is one CAS. One writer wins, the others retry.
- **Real backends.** S3, R2, Tigris, GCS, Azure Blob, MinIO, Postgres, SFTP, fileshare, local disk. Each one passes the same multi-commit, branch, merge, and CAS-conflict contract test.
- **Real scale.** Virtual mode mounts a 100GB repo without materializing it.
- **Real portability.** Same commands on a laptop, EC2, Lambda, Cloudflare Worker, Modal sandbox, Daytona, CI runner.

## Mount Modes

```bash
trunks mount --repo my-app --path ./my-app
trunks mount --repo my-app --path ./my-app --watch
trunks mount --repo my-app --path ./my-app --storage primary
trunks mount --repo big-repo --path ./big-repo --mode virtual
```

Default is plain files. `--watch` keeps a journal so a crash doesn't lose work. `--mode virtual` sparsely materializes huge repos.

`trunks mount` also bootstraps git: it runs `git init --initial-branch=main` if `.git` is missing and adds `origin -> trunks://<storage>/<repo>` if no `origin` is configured. With one primary storage profile this is automatic; pass `--storage <name>` if you have several. Pass `--no-git` to skip the bootstrap.

## Branches Are Pointers

One branch per task. Creating one is a single ref write. No copy.

```bash
trunks branch create --name feature/auth --from main
trunks branch switch --name feature/auth
trunks shell
git add .
git commit -m "update auth"
git push
```

Two writers on different branches don't collide. Two writers on the same branch race a compare-and-swap. One wins. The other retries.

## Git Without GitHub

```bash
git remote add origin trunks://primary/my-app
git push -u origin main
git clone trunks://primary/my-app ./my-app-copy
```

`trunks://primary/my-app` resolves the configured `primary` storage profile and the `my-app` repo namespace. Same commits. Same refs. Your storage.

## Mirrors

Add one primary storage profile and any number of explicit mirrors:

```bash
trunks storage add primary --backend s3 --bucket company-trunks
trunks storage add backup --mirror --backend s3 --bucket company-trunks-backup
git push origin main
```

Pushes to `trunks://primary/<repo>` write through a strict multi-backend path. The primary ref update is CAS-fenced, and configured mirrors receive the same objects, branch refs, and push trigger refs. Inline URLs such as `trunks+s3://bucket/path` target only that one backend.

## Python

Use the Python SDK when your application wants files and commits without shelling out.

```python
from trunks import Trunk

with Trunk(name="my-app") as trunk:
    trunk.write("task.md", b"Fix auth\n")
    trunk.commit(message="update auth")
    trunk.push()
```

```python
async with Trunk(name="my-app") as trunk:
    await trunk.write("task.md", b"Fix auth\n")
    await trunk.commit(message="update auth")
    await trunk.push()
```

## Node

```ts
import { Trunks } from "@layerbrain/trunks";

const trunks = new Trunks();
const fs = await trunks.mount({ repo: "my-app", path: "./my-app", watch: true });

await fs.write("task.md", "Fix auth\n");
await fs.checkpoint("update auth");
await fs.push();
```

## Resource API

CLI, Python, and Node share the same resource API.

```bash
trunks repo create --name my-app --backend s3://company-trunks --json
trunks branch list --json --limit 20 --offset 0
```

```python
client = Trunks(cwd="./my-app")
client.branches.create(name="feature/auth", from_ref="main")
```

```ts
const trunks = new Trunks();
await trunks.branches.create({ name: "feature/auth", from: "main" });
```

List calls return the same envelope everywhere:

```json
{
  "object": "list",
  "data": [],
  "limit": 20,
  "offset": 0,
  "total_count": 0,
  "has_more": false
}
```

## Where To Next

| Want to do | Page |
|---|---|
| Walk through your first repo | [Tutorial](docs/tutorial.md) |
| See every command | [CLI reference](docs/cli.md) |
| Use Python | [Python SDK](docs/sdk-python.md) |
| Use Node | [Node SDK](docs/sdk-node.md) |
| Resource shapes | [Resources](docs/resources.md) |
| Pick a backend | [Backends](docs/backends/README.md) |
| Use Trunks with agent frameworks | [Agents](docs/agents.md) |
| Run CI on your own infrastructure | [Trunks Actions](docs/actions.md) |
| Understand the bytes | [Architecture](docs/architecture.md) |

Backend guides:

- [S3-compatible storage](docs/backends/s3.md)
- [Azure Blob Storage](docs/backends/azure.md)
- [Google Cloud Storage](docs/backends/gcs.md)
- [Postgres](docs/backends/postgres.md)
- [SFTP](docs/backends/sftp.md)
- [Local disk and fileshares](docs/backends/local.md)

Examples:

- [Mastra](examples/mastra-pr-agent/README.md)
- [OpenAI Agents SDK](examples/openai-agents-pr-agent/README.md)
- [LangChain / LangGraph](examples/langchain-pr-agent/README.md)
- [CrewAI](examples/crewai-pr-agent/README.md)
- [Pydantic AI](examples/pydantic-ai-pr-agent/README.md)
- [Agno](examples/agno-pr-agent/README.md)
- [E2B](examples/e2b-sandbox/README.md)
- [Daytona](examples/daytona-sandbox/README.md)
- [Blaxel](examples/blaxel-sandbox/README.md)

Actions:

- [Daytona + MinIO](docs/actions/examples/daytona-minio/README.md)

MIT licensed.
