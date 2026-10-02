"""
Wires every currently-known P2 golden case into a spine GoldenCaseRegistry.
One line per capability, the same shape as P1's p1.eval.registrations: the
harness itself (spine.eval) never changes when this file grows. PM-12 is
the first (GC1, GC2); later rows add theirs here.
"""

from __future__ import annotations

from spine.eval.cases import GoldenCaseRegistry


def register_all(registry: GoldenCaseRegistry, **kwargs) -> None:
    from pm.eval.pm12_cases import register as register_pm12

    register_pm12(registry, **kwargs)
