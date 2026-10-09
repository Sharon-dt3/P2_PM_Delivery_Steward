"""Golden case 3 (PM-07), registered with the harness so it is RECORDED with the others: delta correctness.

The diff engine (PM-06) is graded against the hand-labelled changed set for one real window of the seeded project: precision and recall, both at 1.0, and the
item that moved to done and back the same day (PM-016, the twice-moved item) must appear exactly once, as a flap. A prediction counts as a true positive only when
BOTH the item and the kind (status_changed vs flapped) match a hand label, so calling a flap a status change is a miss, not a match.

The labels are independent of the engine (src/pm/eval/golden_cases.py: written by hand from the seed, never read back from compute_delta). This module only grades.
"""

from __future__ import annotations

from collections import Counter

from spine.eval.cases import (
    GoldenCase,
    GoldenCaseRegistry,
    MetricResult,
    at_least,
    at_most,
)

from pm.eval.golden_cases import GOLDEN_CASE_3
from pm.eval.runner import build_case_3_delta, evaluate_case_3

PRECISION_ID = "GC3-delta-precision"
RECALL_ID = "GC3-delta-recall"
DUPLICATE_ID = "GC3-duplicate-entry-count"


def measure_gc3() -> list[MetricResult]:
    delta = build_case_3_delta()
    result = evaluate_case_3(delta)
    labelled = len(GOLDEN_CASE_3)
    per_item = Counter(entry.item_id for entry in delta.items)
    duplicates = sum(count - 1 for count in per_item.values() if count > 1)  # the same item reported more than once (the twice-moved one, above all)
    wrong = {"missed": result.false_negatives, "extra": result.false_positives}
    return [
        MetricResult(
            metric_id=PRECISION_ID, name="share of the engine's reported changes that are in the hand-labelled changed set",
            measured=result.precision, target=1.0, comparator_name="at_least", passed=at_least(result.precision, 1.0),
            detail=f"{len(result.true_positives)} of {len(delta.items)} reported changes are labelled" + (f"; extra: {wrong['extra']}" if wrong["extra"] else ""),
        ),
        MetricResult(
            metric_id=RECALL_ID, name="share of the hand-labelled changes the engine reported, with the right kind",
            measured=result.recall, target=1.0, comparator_name="at_least", passed=at_least(result.recall, 1.0),
            detail=f"{len(result.true_positives)} of {labelled} labelled changes found" + (f"; missed: {wrong['missed']}" if wrong["missed"] else ""),
        ),
        MetricResult(
            metric_id=DUPLICATE_ID, name="items reported more than once in the delta, the twice-moved item above all (hard zero)",
            measured=duplicates, target=0, comparator_name="at_most", passed=at_most(duplicates, 0),
            detail=f"{len(per_item)} items reported, each once" if not duplicates else f"reported more than once: {sorted(i for i, c in per_item.items() if c > 1)}",
        ),
    ]


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC3",
            description="Delta correctness: precision and recall of the computed changed set against the hand labels, the twice-moved item reported once",
            measure_fn=measure_gc3,
        )
    )
