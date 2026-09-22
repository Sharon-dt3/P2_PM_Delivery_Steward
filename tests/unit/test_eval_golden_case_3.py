"""PM-07: golden case 3, delta correctness.

Row-level acceptance test: "Precision and recall printed against hand
labels." This asserts exact-match precision/recall on the same
build_case_3_delta()/evaluate_case_3() the runner's own main() prints, so
the printed report and this assertion can never silently drift apart --
capsys captures the actual stdout print_report() produces, so the
"printed" half of the acceptance test is checked too, not just the score.
"""

from __future__ import annotations

from pm.eval.golden_cases import GOLDEN_CASE_3
from pm.eval.runner import build_case_3_delta, evaluate_case_3, print_report


def test_engine_matches_every_hand_label_exactly(seeded_db_path):
    """Depends on PM-06: the delta this grades is compute_delta()'s real
    output, not a stand-in."""
    delta = build_case_3_delta(db_path=seeded_db_path)
    result = evaluate_case_3(delta)

    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.false_positives == []
    assert result.false_negatives == []
    assert result.true_positives == ["PM-016", "PM-018", "PM-020"]


def test_the_delta_contains_nothing_beyond_the_three_hand_labeled_items(seeded_db_path):
    """A stricter check than precision==1.0 alone (which would also be
    1.0 if the engine reported zero items and only recall suffered):
    confirms the engine actually reported exactly these three, not none."""
    delta = build_case_3_delta(db_path=seeded_db_path)
    assert {item.item_id for item in delta.items} == {"PM-016", "PM-018", "PM-020"}


def test_report_prints_the_score_and_each_hand_label(seeded_db_path, capsys):
    delta = build_case_3_delta(db_path=seeded_db_path)
    result = evaluate_case_3(delta)
    print_report(delta, result)

    captured = capsys.readouterr()
    assert "precision: 1.000" in captured.out
    assert "recall:    1.000" in captured.out
    for label in GOLDEN_CASE_3:
        assert label.item_id in captured.out


def test_a_deliberately_wrong_hand_label_is_caught_as_a_false_negative(seeded_db_path):
    """Proves evaluate_case_3() actually grades rather than always
    reporting a perfect score -- feed it a label the engine cannot
    possibly match (a status change for an item with no transitions in
    the window at all) and confirm it shows up as missed, dragging recall
    below 1.0."""
    from pm.eval.golden_cases import HandLabel
    from pm.state.diff import STATUS_CHANGED

    delta = build_case_3_delta(db_path=seeded_db_path)
    bogus_labels = GOLDEN_CASE_3 + [
        HandLabel(
            item_id="PM-001",
            kind=STATUS_CHANGED,
            before_status="in_progress",
            after_status="done",
            note="Deliberately wrong -- PM-001 finished in August, long before this window.",
        )
    ]

    result = evaluate_case_3(delta, labels=bogus_labels)
    assert "PM-001" in result.false_negatives
    assert result.recall < 1.0
