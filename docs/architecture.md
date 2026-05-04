# Architecture

One storage model. Many ways to talk to it.

## The Whole Stack

```text
   ┌────────────────────────────────────────────────────┐
   │  CLI · Python SDK · Node SDK · Git shell · mount   │   surfaces
   └─────────────────────────┬──────────────────────────┘
                             │
                             ▼
            ┌─────────────────────────────────┐
            │     local Trunks repo cache     │   on this machine
            │   objects · refs · journal      │
            └────────────────┬────────────────┘
                             │  push / pull
                             ▼
       ┌────────────────────────────────────────────┐
       │       your configured backend              │   the remote
       │  S3 · GCS · Azure · Postgres · SFTP ·      │
       │  fileshare · local disk                    │
       └────────────────────────────────────────────┘
```

The backend is the protocol. Two machines sync by reading and writing the same backend prefix. There is nothing else to run.

## Storage Layout

```text
s3://company-trunks/trunks/my-app.trunk/
├── objects/         content-addressed blobs, trees, commits
│   ├── 9f/
│   │   └── 86a8d...     ← sha-256 keyed
│   └── ...
├── refs/
│   └── heads/
│       ├── main         ← contains commit sha
│       └── feature/auth
└── journals/
    └── 2024-09-...      ← append-only durability records
```

## The Object Graph

```text
   refs/heads/main
        │
        ▼
   ┌──────────┐    parent
   │ commit B │ ─────────────┐
   └─────┬────┘              │
         │ tree              ▼
         ▼              ┌──────────┐
   ┌──────────┐         │ commit A │
   │  tree X  │         └─────┬────┘
   └─────┬────┘               │ tree
         │                    ▼
   ┌─────┴─────┐         ┌──────────┐
   ▼           ▼         │  tree Y  │
┌──────┐   ┌──────┐      └──────────┘
│ blob │   │ blob │
└──────┘   └──────┘
```

A commit points to one tree (the file system) and zero or more parent commits. Trees point to blobs (files) and other trees (subdirectories). Every object is keyed by the SHA of its content.

## Mount Modes

```bash
trunks mount --repo my-app --path ./my-app
```

Default mount writes the working tree to plain files on disk. Tools use `cat`, `vim`, `npm`, `python`.

```bash
trunks mount --repo big-repo --path ./big-repo --mode virtual
```

Virtual mount starts a local filesystem daemon. The folder behaves like a real folder, but blobs only get fetched when something reads them. Use this for repos too big to materialize.

```text
   user app                                user app
       │                                       │
       │ open/read                             │ open/read
       ▼                                       ▼
   real files on disk                  OS filesystem client
                                              ▼
                                       trunks daemon
                                              │
                                       fetch blob on demand
                                              ▼
                                          backend
```
## Git Compatibility

```bash
cd ./my-app
trunks shell
git commit -m "update auth"
git push
```

`trunks shell` opens a shell with Trunks Git interop enabled. It creates a `.git` cache only when Trunks owns that cache; existing real Git repositories are left alone. Trunks writes the same objects and refs as `trunks checkpoint` would have.

You can read the repo with `git log`, diff with `git diff`, or never touch Git.

## Concurrency

```text
   writer A                              writer B
      │                                    │
      │ commit + push                      │ commit + push
      ▼                                    ▼
       both want to advance refs/heads/main
                          │
                          ▼
                  ┌────────────────┐
                  │      CAS       │
                  │ expected: SHA-X│
                  │ new:      SHA-Y│
                  └───────┬────────┘
                          │
              one wins ──┴── one fails
                                    │
                                    ▼
                            pull, retry
```

Objects are immutable, so any number of writers can upload blobs simultaneously without conflict. Refs are guarded by compare-and-swap. Two writers on the same branch race. One wins. The other gets a clean failure and retries.

Two writers on different branches never race at all.

## Cache

Backend reads go through a local cache:

```text
   read("README.md")
        │
        ▼
   ┌─────────────────┐
   │ object cache    │ → hit: return bytes
   │ (sha-keyed,     │
   │  immutable)     │ → miss
   └────────┬────────┘
            │
            ▼
        backend
            │
            ▼
   verify SHA, store in cache, return
```

Objects are immutable, so they're cached forever and verified by hash on read. Refs are mutable, so they use a short TTL and are invalidated on push, pull, and fetch.
