"""
Golden-case registry (SPN-07): the metric-registry half of the eval
harness framework. A GoldenCase is a named, registered measurement --
CHN-11's GC1/GC2, CHN-12's GC5/GC10, and their P2/P3 equivalents later
all plug into this same mechanism, and none of it is specific to
Teams, messages, or any P1 concept. Reused by P2 and P3 with new cases
only, per this task's own WBS row -- nothing here should ever need to
change for a new agent to add its own golden cases.

A case's measure_fn does the actual work (querying whatever store,
running whatever pipeline) and returns a list of MetricResults -- most
cases report exactly one metric, but a case that reports several
related numbers at once (e.g. GC1's gated precision alongside its
merely-reported recall) returns more than one, all still filed under
the same case_id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


def at_least(measured: float, target: float) -> bool:
    return measured >= target


def at_most(measured: float, target: float) -> bool:
    return measured <= target


def equals(measured: object, target: object) -> bool:
    return measured == target


@dataclass(frozen=True)
class MetricResult:
    """One printed/recorded line: a single named value checked against
    its target with a named comparator. comparator_name is a plain
    label ("at_least", "equals", ...) for display and for the results
    file -- it does not need to be one of the three helpers above; a
    case is free to compute passed however makes sense for it and just
    label the comparison it used."""

    metric_id: str
    name: str
    measured: object
    target: object
    comparator_name: str
    passed: bool
    detail: str = ""

    def format_line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        detail = f" ({self.detail})" if self.detail else ""
        return (
            f"{self.metric_id} {self.name}: measured={self.measured!r} "
            f"target[{self.comparator_name}]={self.target!r} {status}{detail}"
        )


@dataclass(frozen=True)
class GoldenCase:
    """A registered golden case. measure_fn takes no arguments -- any
    context it needs (a db_path, a gateway, fixture data) must already
    be bound into it by whoever registers it, e.g. via functools.partial
    or a small closure in that capability's own registration module."""

    case_id: str
    description: str
    measure_fn: Callable[[], list[MetricResult]]


class GoldenCaseRegistry:
    """Deliberately instantiable, not a single module-level global --
    every eval run builds its own registry and registers into it, so
    tests (and separate P2/P3 registrations later) never share mutable
    state with each other."""

    def __init__(self) -> None:
        self._cases: dict[str, GoldenCase] = {}

    def register(self, case: GoldenCase) -> None:
        if case.case_id in self._cases:
            raise ValueError(f"duplicate golden case id: {case.case_id!r}")
        self._cases[case.case_id] = case

    def get(self, case_id: str) -> GoldenCase:
        try:
            return self._cases[case_id]
        except KeyError:
            raise KeyError(f"no golden case registered with id={case_id!r}") from None

    def all_cases(self) -> list[GoldenCase]:
        """Sorted by case_id -- a run's output and results-file order is
        deterministic, never an artefact of registration order."""
        return [self._cases[case_id] for case_id in sorted(self._cases)]