# Memory backend

A backend that lives in RAM and disappears when your process exits. Use it for tests.

## URL form

```
memory://
memory                  same thing
```

There's nothing to configure. There are no credentials. There is no persistence.

## Quick start

```python
from trunks import Trunk

with Trunk(backend="memory://") as trunk:
    trunk.write("README.md", b"# hi\n")
    commit = trunk.commit(message="init")
    print(commit.id)
```

When the `with` block exits, the backend is gone.

## What it's for

- Unit tests that need a real `Trunk` without touching disk.
- Quick experiments at a Python REPL.
- Continuous integration where you don't want a Postgres or MinIO container.

## What it's not for

- Anything you want to keep.
- Multiple processes (each one gets its own RAM-only backend; they don't see each other).
- Anything where you'd notice the data being gone after a crash.

## Notes

The memory backend implements every capability the bigger backends do — CAS refs, journal, list-prefix, read-after-write — so a test that passes against `memory://` will pass against S3 unless it depends on something memory does not have (notably `Capabilities.locks`, which only S3-style backends advertise).
