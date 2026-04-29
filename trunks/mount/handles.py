"""NFSv3 file handle table.

NFS handles are opaque to the client and stable for the file's lifetime. We
derive a deterministic 32-byte handle from the path so handles survive daemon
restart. The reverse table populates lazily on first observation; renames and
deletes drop the old entry.
"""

from __future__ import annotations

import hashlib
import os
import threading

ROOT_PATH = ""
HANDLE_LEN = 32


class HandleTable:
    """Maps NFS handles to repository paths."""

    def __init__(self) -> None:
        self._secret = os.urandom(32)
        self._lock = threading.Lock()
        self._fwd: dict[bytes, str] = {}
        # Pre-register the root handle.
        self._fwd[self._derive(ROOT_PATH)] = ROOT_PATH

    def _derive(self, path: str) -> bytes:
        return hashlib.sha256(self._secret + b"\x00" + path.encode("utf-8")).digest()[:HANDLE_LEN]

    def root(self) -> bytes:
        return self._derive(ROOT_PATH)

    def register(self, path: str) -> bytes:
        handle = self._derive(path)
        with self._lock:
            self._fwd[handle] = path
        return handle

    def resolve(self, handle: bytes) -> str | None:
        if len(handle) != HANDLE_LEN:
            return None
        with self._lock:
            return self._fwd.get(handle)

    def forget(self, path: str) -> None:
        handle = self._derive(path)
        with self._lock:
            self._fwd.pop(handle, None)

    def rename(self, old_path: str, new_path: str) -> None:
        with self._lock:
            self._fwd.pop(self._derive(old_path), None)
            self._fwd[self._derive(new_path)] = new_path

    def fileid(self, path: str) -> int:
        """Return a stable 64-bit fileid for a path (used as inode-equivalent)."""
        digest = hashlib.sha256(path.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")
