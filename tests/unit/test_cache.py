from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.backends.memory import Memory
from trunks.cache import CacheManager, CachedBackend
from trunks.cli import dispatch
from trunks.errors import ObjectNotFound
from trunks.ids import ObjectId


class CountingMemory(Memory):
    def __init__(self) -> None:
        super().__init__()
        self.object_reads = 0

    async def read_object(self, oid: ObjectId) -> bytes:
        self.object_reads += 1
        return await super().read_object(oid)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_backend_serves_repeated_object_reads_from_disk_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = CountingMemory()
            cache = CacheManager(tmp)
            oid = ObjectId.from_bytes(b"hello")
            await backend.write_object(oid, b"hello")
            cached = CachedBackend(backend, cache)

            self.assertEqual(await cached.read_object(oid), b"hello")
            self.assertEqual(await cached.read_object(oid), b"hello")

            self.assertEqual(backend.object_reads, 1)
            stats = cache.stats()
            self.assertEqual(stats.hits, 1)
            self.assertEqual(stats.misses, 1)

    async def test_cached_backend_records_negative_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = CountingMemory()
            cache = CacheManager(tmp)
            cached = CachedBackend(backend, cache)
            oid = ObjectId.from_bytes(b"missing")

            with self.assertRaises(ObjectNotFound):
                await cached.read_object(oid)
            with self.assertRaises(ObjectNotFound):
                await cached.read_object(oid)

            self.assertEqual(backend.object_reads, 1)
            self.assertEqual(cache.stats().negative_hits, 1)

    async def test_cache_cli_stats_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TRUNKS_CACHE_DIR": tmp}, clear=False):
            cache = CacheManager()
            oid = ObjectId.from_bytes(b"cached")
            cache.put_object(oid, b"cached")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(await dispatch(["cache", "stats", "--json"]), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["objects"], 1)

            with redirect_stdout(io.StringIO()) as verify_out:
                self.assertEqual(await dispatch(["cache", "verify"]), 0)
            self.assertIn("ok", verify_out.getvalue())

    async def test_cache_cli_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TRUNKS_CACHE_DIR": tmp}, clear=False):
            cache = CacheManager()
            oid = ObjectId.from_bytes(b"cached")
            cache.put_object(oid, b"cached")
            self.assertTrue(any(Path(tmp).rglob("*")))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(await dispatch(["cache", "clear"]), 0)
            self.assertFalse(any(path.is_file() for path in Path(tmp).rglob("*")))


if __name__ == "__main__":
    unittest.main()
