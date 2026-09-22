"""
Golden case 3 runner (PM-07).

Builds the real "morning" and "end of day" snapshots over this repo's own
seeded database, runs the real diff engine (PM-06's compute_delta()) over
them, and scores what it found against golden_cases.GOLDEN_CASE_3's hand
labels -- printing a precision/recall report, exactly as the row's own
acceptance test asks: "Report precision and recall."

Run via scripts/run_eval.py for a human-readable demo:
    uv run python scripts/run_eval.py
(not `uv run python -m pm.eval.runner` directly -- this repo's own path
dependencies on p1/spine only resolve inside pytest's own `pythonpath` ini
option or a script that inserts those paths itself; scripts/run_eval.py
does that the same way scripts/seed.py already does. See that script's
own comment.)

tests/unit/test_eval_golden_case_3.py asserts on the same evaluate_case_3()
result this prints, so the printed report and the graded assertion can
never drift apart.
"""

from __future__ import annotations

from pydantic import BaseModel

from pm.adapters.code_host import CodeHostMock
from pm.adapters.teams import get_teams_reader
from pm.adapters.tracker import TrackerMock
from pm.eval.golden_cases import END_OF_DAY, GOLDEN_CASE_3, MORNING, HandLabel
from pm.seed.build import CHANNEL_ID
from pm.state.diff import SnapshotDelta, compute_delta
from pm.state.snapshot import build_snapshot
from pm.storage.db import DEFAULT_DB_PATH


class EvaluationResult(BaseModel):
    precision: float
    recall: float
    true_positives: list[str]  # item ids
    false_positives: list[str]  # item ids the engine flagged that aren't hand-labeled
    false_negatives: list[str]  # hand-labeled item ids the engine missed


def build_case_3_delta(db_path=None) -> SnapshotDelta:
    """Builds the two real snapshots bracketing golden case 3's window and
    returns the actual computed delta between them -- the thing being
    graded, built from this repo's own real adapters over its own real
    seeded data, not a stand-in."""
    resolved_db_path = db_path if db_path is not None else DEFAULT_DB_PATH
    tracker = TrackerMock(db_path=resolved_db_path)
    code_host = CodeHostMock(db_path=resolved_db_path)
    teams_reader = get_teams_reader(db_path=resolved_db_path)

    before = build_snapshot(tracker, code_host, teams_reader, CHANNEL_ID, taken_at=MORNING)
    after = build_snapshot(tracker, code_host, teams_reader, CHANNEL_ID, taken_at=END_OF_DAY)
    return compute_delta(before, after, tracker)


def evaluate_case_3(delta: SnapshotDelta, labels: list[HandLabel] = GOLDEN_CASE_3) -> EvaluationResult:
    """Scores an actual SnapshotDelta against hand labels. A prediction
    counts as a true positive only when BOTH the item id and the kind
    (status_changed vs. flapped) match a hand label -- getting the right
    item but calling a flap a status change (or vice versa) is exactly
    the mistake this golden case exists to catch, so it counts as a
    miss, not a match."""
    predicted = {(item.item_id, item.kind) for item in delta.items}
    expected = {label.key for label in labels}

    true_positives = sorted(item_id for item_id, _ in predicted & expected)
    false_positives = sorted(item_id for item_id, _ in predicted - expected)
    false_negatives = sorted(item_id for item_id, _ in expected - predicted)

    precision = len(predicted & expected) / len(predicted) if predicted else 1.0
    recall = len(predicted & expected) / len(expected) if expected else 1.0

    return EvaluationResult(
        precision=precision,
        recall=recall,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
    )


def _describe(delta: SnapshotDelta, item_id: str) -> str:
    for item in delta.items:
        if item.item_id == item_id:
            return item.description
    return "(not reported)"


def print_report(delta: SnapshotDelta, result: EvaluationResult, labels: list[HandLabel] = GOLDEN_CASE_3) -> None:
    print(f"Golden case 3 -- window {delta.before_taken_at} .. {delta.after_taken_at}")
    print(f"  hand-labeled changes : {len(labels)}")
    print(f"  engine-reported changes: {len(delta.items)}")
    print()
    for label in labels:
        mark = "OK" if label.item_id in result.true_positives else "MISS"
        print(f"  [{mark}] {label.item_id} ({label.kind}): {label.note}")
        print(f"        engine said: {_describe(delta, label.item_id)}")
    if result.false_positives:
        print()
        print("  Unexpected (engine reported, not hand-labeled):")
        for item_id in result.false_positives:
            print(f"    - {item_id}: {_describe(delta, item_id)}")
    print()
    print(f"  precision: {result.precision:.3f}")
    print(f"  recall:    {result.recall:.3f}")


def main() -> None:
    delta = build_case_3_delta()
    result = evaluate_case_3(delta)
    print_report(delta, result)


if __name__ == "__main__":
    main()
