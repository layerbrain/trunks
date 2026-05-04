from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconcileDecision:
    should_reconcile: bool
    reason: str


def bounded_reconcile_decision(cas_attempts: int, *, max_inline_attempts: int = 3) -> ReconcileDecision:
    if cas_attempts < max_inline_attempts:
        return ReconcileDecision(False, "inline")
    return ReconcileDecision(True, "cas-contention")
