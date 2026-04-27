from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.backends.local import Local
from trunks.chunked import CHUNK_THRESHOLD
from trunks.engine import Engine
from trunks.errors import BackendUnavailable
from trunks.objects import Blob
from trunks.repository import Repository
from tests.contract.backend import assert_backend_contract


class LocalBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = Local(tmp)
            await assert_backend_contract(self, backend)

    async def test_push_writes_segment_not_loose_object_fanout(self) -> None:
        with tempfile.TemporaryDirectory() as remote, tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as clone:
            backend = Local(remote)
            repo = Repository.init(workdir)
            engine = Engine(repo, backend)
            for index in range(20):
                await engine.write(f"src/file-{index}.txt", f"file {index}\n".encode())
            commit = await engine.commit(message="many files")
            await engine.push()

            root = Path(remote)
            self.assertEqual(list((root / "objects").rglob("*")) if (root / "objects").exists() else [], [])
            self.assertGreaterEqual(len(list((root / "segments").glob("*.tseg"))), 1)
            self.assertGreaterEqual(len(list((root / "segments").glob("*.tidx"))), 1)

            pulled = Repository.init(clone)
            await Engine(pulled, backend).pull()

            self.assertEqual(pulled.ref("main"), commit.id)
            self.assertEqual((Path(clone) / "src" / "file-19.txt").read_text(), "file 19\n")

    async def test_large_object_uses_chunks_and_repairs_missing_chunk_on_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as remote:
            backend = Local(remote)
            blob = Blob.from_data(b"x" * (CHUNK_THRESHOLD + 17))
            await backend.write_object(blob.id, blob.canonical())

            root = Path(remote)
            self.assertTrue((root / "objects" / blob.id.value[:2] / f"{blob.id}.tmanifest").exists())
            chunks = list((root / "chunks").rglob("*"))
            chunk_files = [path for path in chunks if path.is_file()]
            self.assertGreater(len(chunk_files), 1)
            self.assertEqual(await backend.read_object(blob.id), blob.canonical())

            chunk_files[0].unlink()
            with self.assertRaisesRegex(BackendUnavailable, "missing chunk"):
                await backend.read_object(blob.id)

            await backend.write_object(blob.id, blob.canonical())
            self.assertEqual(await backend.read_object(blob.id), blob.canonical())

            chunk_files[1].write_bytes(b"corrupt")
            with self.assertRaisesRegex(BackendUnavailable, "corrupt chunked object"):
                await backend.read_object(blob.id)

            await backend.write_object(blob.id, blob.canonical())
            self.assertEqual(await backend.read_object(blob.id), blob.canonical())


if __name__ == "__main__":
    unittest.main()
