# Memory

Use `memory://` for tests and short REPL experiments.

```python
from trunks import Trunk

with Trunk(backend="memory://") as trunk:
    trunk.write("README.md", b"# hi\n")
    commit = trunk.commit(message="init")
    print(commit.id)
```

The data disappears when the process exits. Do not use it for durable agent work.
