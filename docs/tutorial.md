# Tutorial

This walks through the normal path: connect storage once, keep using Git.

## Install

```bash
pip install trunks
```

## Connect Storage

Pick a backend your team already controls.

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1

mkdir myrepo && cd myrepo
git init
trunks init
trunks storage add --name primary --backend s3 --bucket my-bucket
```

`s3://my-bucket` is the backend root. The actual trunk for this repo is resolved from the folder name:

```text
s3://my-bucket/trunks/myrepo.trunk/
```

`trunks storage add --name primary ...` validates the backend before saving it. It writes and reads a small test object, checks listing, and writes a journal record.

Trunks saves storage names and locations in `.trunks/myrepo.trunk`. If you store credentials with `trunks storage add`, they stay in that local database and are not written to the remote backend. A new sandbox can either receive credentials from its environment, CI secrets, IAM role, SSH agent, or run `trunks storage add` once for its own local profile.

Check it again later with:

```bash
trunks storage ping primary
```

## Use Git

```bash
echo "# hi" > README.md
git add README.md
git commit -m "first"
git push
```

The push syncs to the connected Trunks backend.

No Git origin is required. If you have connected Trunks storage, `git push` writes there by default.

## Add A Mirror

If you want every push copied to another backend:

```bash
trunks storage add --name backup --backend s3 --bucket my-backup-bucket --mirror
trunks storage ping backup
```

Primary storage decides whether a push is accepted. Mirrors are copied in the same push, and a mirror failure makes the Trunks push fail so you do not get a fake "synced" state.

## Pull Somewhere Else

```bash
mkdir myrepo-copy && cd myrepo-copy
trunks init --name myrepo --backend s3://my-bucket/trunks/myrepo.trunk
trunks pull
git log
```

That is the agent-to-agent path too. A new sandbox only needs the primary trunk URL. After `trunks pull`, it learns the storage map stored in the trunk, including mirrors, before it pushes new work.

## Use It From Python

```python
from trunks import Trunk

with Trunk(backend="s3://my-bucket", name="myrepo") as trunk:
    trunk.pull()
    print(trunk.read("README.md"))
```

That loads `s3://my-bucket/trunks/myrepo.trunk/`.

## Check And Clean

```bash
trunks check
trunks check --clean
```

`check` verifies the local repo and rebuilds Git compatibility state. `--clean` also removes unreachable local objects.

## Next

Read the per-backend pages in [docs/backends/](backends/) for credentials and URL formats.
