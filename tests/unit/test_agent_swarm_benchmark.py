from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from benchmarks.agent_swarm import run_trunks_benchmark


class AgentSwarmBenchmarkTests(unittest.TestCase):
    def test_independent_branches_all_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_trunks_benchmark(
                Path(tmp),
                scenario="branches",
                changes=12,
                workers=4,
                payload_bytes=128,
            )

        self.assertEqual(result.successes, 12)
        self.assertEqual(result.failures, 0)
        self.assertGreater(result.changes_per_second, 0)

    def test_same_branch_contention_fails_without_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_trunks_benchmark(
                Path(tmp),
                scenario="same-branch",
                changes=12,
                workers=4,
                payload_bytes=128,
            )

        self.assertEqual(result.successes, 1)
        self.assertEqual(result.failures, 11)
        self.assertIn("sample failure", result.detail)


if __name__ == "__main__":
    unittest.main()
