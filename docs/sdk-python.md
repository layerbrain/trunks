# Python SDK

```bash
pip install trunks
```

Use the Python SDK from scripts, services, notebooks, or agent runtimes.

## Files

```python
from trunks import Trunk

with Trunk(name="my-app") as trunk:
    trunk.pull()
    trunk.write("task.md", b"Fix auth\n")
    trunk.commit(message="agent output")
    trunk.push()
```

Event-loop runtimes use the same object with `await`:

```python
async with Trunk(name="my-app") as trunk:
    await trunk.pull()
    await trunk.write("task.md", b"Fix auth\n")
    await trunk.commit(message="agent output")
    await trunk.push()
```

`name` is the repo identity. Storage comes from local Trunks config, `TRUNKS_REPO`, `TRUNKS_BACKEND`, or the default backend you configured with `trunks repo create`.

## File Methods

```python
trunk.read("README.md")
trunk.write("task.md", b"Fix auth\n")
trunk.list("src")
trunk.exists("pyproject.toml")
trunk.copy("a.txt", "b.txt")
trunk.move("b.txt", "archive/b.txt")
trunk.mkdir("archive")
trunk.remove("archive/b.txt")
```

These methods are available in regular scripts and event-loop runtimes.

## Versioning

```python
trunk.commit(message="agent output")
trunk.log()                 # iterate commits, newest first
trunk.status()              # what's pending vs HEAD
trunk.push()
trunk.pull()
trunk.fetch()
```

## Resource API

The Stripe-shaped client for repos, branches, tags, storage, webhooks, audit:

```python
from trunks import Trunks

client = Trunks(cwd="./my-app")

repos = client.repos.list(limit=20, offset=0)
branches = client.branches.list(limit=20, offset=0)
branch = client.branches.create(name="agent/run-7", from_ref="main")
events = client.audit.list(limit=10, offset=0)
```

```python
import asyncio
from trunks import Trunks

async def main():
    client = Trunks(cwd="./my-app")
    repos = await client.repos.list(limit=20, offset=0)

asyncio.run(main())
```

See [Resources](resources.md) for the full surface.
