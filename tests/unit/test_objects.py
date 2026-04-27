from __future__ import annotations

import unittest
from datetime import UTC, datetime

from trunks.objects import Blob, Commit, Identity, Tree, TreeEntry, parse_canonical_object


class ObjectTests(unittest.TestCase):
    def test_blob_id_is_git_compatible(self) -> None:
        blob = Blob.from_data(b"hello\n")
        self.assertEqual(str(blob.id), "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertEqual(parse_canonical_object(blob.canonical()), ("blob", b"hello\n"))

    def test_commit_excludes_labels_from_hash(self) -> None:
        blob = Blob.from_data(b"hello\n")
        tree = Tree.from_entries([TreeEntry("100644", "README.md", blob.id)])
        identity = Identity("Alice", "alice@example.com", datetime(2026, 1, 1, tzinfo=UTC))
        commit_a = Commit.create(tree=tree.id, parents=[], author=identity, committer=identity, message="init")
        commit_b = Commit.create(tree=tree.id, parents=[], author=identity, committer=identity, message="init")
        self.assertEqual(commit_a.id, commit_b.id)


if __name__ == "__main__":
    unittest.main()

