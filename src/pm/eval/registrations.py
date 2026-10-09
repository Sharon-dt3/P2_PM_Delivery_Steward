"""
Wires every currently-known P2 golden case into a spine GoldenCaseRegistry.
One line per capability, the same shape as P1's p1.eval.registrations: the
harness itself (spine.eval) never changes when this file grows. PM-07 adds GC3 (delta correctness); PM-12 is
the first (GC1, GC2); PM-14 adds GC6 (approval enforcement); later rows add
theirs here. PM-18 adds GC4 (risk-log gap precision/recall and no duplicate); PM-20 adds GC5 (promotion threshold reconfiguration); PM-23 adds GC9 (determinism of the brief's facts); PM-25 adds GC7 (the shared nudge cap and the order of reminder and escalation); PM-27 adds GC8 (scope and consent refusal); PM-35 adds its seven edge scenarios to GC2 as probes (the fabrication count stays one number).
"""

from __future__ import annotations

from spine.eval.cases import GoldenCaseRegistry


def register_all(registry: GoldenCaseRegistry, **kwargs) -> None:
    from pm.eval.pm12_cases import register as register_pm12

    register_pm12(registry, **kwargs)

    from pm.eval.pm35_cases import register as register_pm35  # PM-35: seven edge scenarios, each a probe inside GC2

    register_pm35()

    from pm.eval.pm07_cases import register as register_pm07

    register_pm07(registry)

    from pm.eval.pm14_cases import register as register_pm14

    register_pm14(registry)

    from pm.eval.pm18_cases import register as register_pm18

    register_pm18(registry)

    from pm.eval.pm20_cases import register as register_pm20

    register_pm20(registry)

    from pm.eval.pm23_cases import register as register_pm23

    register_pm23(registry)

    from pm.eval.pm25_cases import register as register_pm25

    register_pm25(registry)

    from pm.eval.pm27_cases import register as register_pm27

    register_pm27(registry)
