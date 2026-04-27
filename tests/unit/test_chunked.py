from __future__ import annotations

import unittest

from trunks.chunked import assemble_chunked_object, decode_manifest, encode_chunked_object
from trunks.objects import Blob


class ChunkedObjectTests(unittest.TestCase):
    def test_chunked_object_round_trips_and_verifies_chunks(self) -> None:
        blob = Blob.from_data((b"abc123" * 1024) + b"tail")
        manifest, chunks = encode_chunked_object(blob.id, blob.canonical(), chunk_size=1024)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(assemble_chunked_object(manifest, [chunk for _, chunk in chunks]), blob.canonical())
        self.assertEqual(decode_manifest(manifest).oid, blob.id)

    def test_chunked_object_rejects_corrupt_chunk(self) -> None:
        blob = Blob.from_data(b"x" * 4096)
        manifest, chunks = encode_chunked_object(blob.id, blob.canonical(), chunk_size=1024)
        corrupt = list(chunks)
        corrupt[0] = (corrupt[0][0], b"y" + corrupt[0][1][1:])

        with self.assertRaises(ValueError):
            assemble_chunked_object(manifest, [chunk for _, chunk in corrupt])


if __name__ == "__main__":
    unittest.main()
