from __future__ import annotations

import unittest

from trunks.errors import ObjectNotFound
from trunks.objects import Blob, Tree, TreeEntry
from trunks.segment import decode_index, encode_segment, read_segment_object


class SegmentTests(unittest.TestCase):
    def test_segment_round_trips_multiple_git_object_kinds(self) -> None:
        blob = Blob.from_data(b"hello")
        tree = Tree.from_entries([TreeEntry("100644", "hello.txt", blob.id)])

        segment_id, segment, index = encode_segment([
            (blob.id, blob.canonical()),
            (tree.id, tree.canonical()),
        ])

        self.assertEqual(len(segment_id), 64)
        self.assertEqual(read_segment_object(segment, index, blob.id), blob.canonical())
        self.assertEqual(read_segment_object(segment, index, tree.id), tree.canonical())
        self.assertEqual({entry.kind for entry in decode_index(index).entries}, {"blob", "tree"})

    def test_segment_rejects_corrupt_data(self) -> None:
        blob = Blob.from_data(b"hello")
        _, segment, index = encode_segment([(blob.id, blob.canonical())])
        corrupt = segment[:-1] + b"x"

        with self.assertRaises(ValueError):
            read_segment_object(corrupt, index, blob.id)

    def test_segment_reports_missing_object(self) -> None:
        blob = Blob.from_data(b"hello")
        other = Blob.from_data(b"other")
        _, segment, index = encode_segment([(blob.id, blob.canonical())])

        with self.assertRaises(ObjectNotFound):
            read_segment_object(segment, index, other.id)


if __name__ == "__main__":
    unittest.main()
