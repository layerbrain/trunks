"""Fast-by-default primitives.

Every asyncio entry point in trunks (CLI dispatch, daemon supervisor, NFS
server, Trunk sync wrapper) imports this module first. The side effect is
a global event-loop policy swap to uvloop on POSIX. Platform invariant,
not a runtime fallback: uvloop has no Windows port, so the import is
guarded by `sys.platform`.

orjson is a hard dependency. The thin `dumps` / `loads` helpers normalize
the on-wire shape used by the JSON-RPC daemon, the journal, and the audit
log: bytes in / bytes out, sorted keys for deterministic framing, UTF-8
strings allowed in keys.
"""

from __future__ import annotations

import sys
from typing import Any

import orjson

if sys.platform != "win32":
    import uvloop

    uvloop.install()


def dumps(obj: Any) -> bytes:
    """Serialize a JSON-shaped Python object to bytes.

    Sorted keys keep wire framing deterministic for tests and signed
    payloads (audit log, webhook signatures).
    """
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)


def loads(data: bytes | str) -> Any:
    """Parse JSON bytes (or str) into a Python object."""
    return orjson.loads(data)
