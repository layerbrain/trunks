from __future__ import annotations

import unittest

from trunks.actions.workflow_reconcile import bounded_reconcile_decision


class WorkflowReconcileTests(unittest.TestCase):
    def test_bounded_reconcile_decision_stays_inline_under_retry_budget(self) -> None:
        decision = bounded_reconcile_decision(2)
        self.assertFalse(decision.should_reconcile)
        self.assertEqual(decision.reason, "inline")

    def test_bounded_reconcile_decision_reconciles_after_retry_budget(self) -> None:
        decision = bounded_reconcile_decision(3)
        self.assertTrue(decision.should_reconcile)
        self.assertEqual(decision.reason, "cas-contention")


if __name__ == "__main__":
    unittest.main()
