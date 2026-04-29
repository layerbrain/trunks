# trunks-python

Python SDK package alias for Trunks.

```bash
pip install trunks-python
```

Import from `trunks`:

```python
from trunks import Trunk

with Trunk(name="my-app") as trunk:
    trunk.write("task.md", b"Fix auth\n")
    trunk.commit(message="agent output")
    trunk.push()
```
