from __future__ import annotations

import unittest

from trunks.ids import ObjectId
from trunks.refs import RefStore, branch_name, normalize_ref


class RefTests(unittest.TestCase):
    def test_normalizes_branch_names(self) -> None:
        self.assertEqual(normalize_ref("main"), "refs/heads/main")
        self.assertEqual(branch_name("refs/heads/main"), "main")

    def test_ref_cas(self) -> None:
        store = RefStore()
        first = ObjectId("0" * 40)
        second = ObjectId("1" * 40)
        self.assertTrue(store.cas("main", None, first))
        self.assertFalse(store.cas("main", None, second))
        self.assertTrue(store.cas("main", first, second))
        self.assertEqual(store.get("main"), second)


if __name__ == "__main__":
    unittest.main()

