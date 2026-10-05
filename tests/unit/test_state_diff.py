"""PM-06: the snapshot diff engine.

The seeded dataset is static -- nothing mutates the database between a
"morning" and an "end of day" build_snapshot() call in these tests, so
each snapshot's own NormalizedItem.status is identical either way. The
whole point of this module is that compute_delta() does NOT rely on that:
it reconstructs each item's status as of each snapshot's own taken_at from
the tracker's real transition history, which is what lets it find real
changes on data that never itself changes.

The two boundaries below (2026-09-15T23:59:59+00:00 /
2026-09-16T23:59:59+00:00) bracket exactly the three items the master
plan's own golden case 3 (PM-07) hand-labels as having changed on
2026-09-16: PM-016 (flapped: done and back, same day -- PM-03 difficulty
2/10), PM-018 (in_progress -> in_review), and PM-020 (backlog ->
in_progress). Every other seeded item's transitions sit entirely outside
this window, so nothing else should show up.
"""

from __future__ import annotations

from pm.adapters.code_host import CodeHostMock
from pm.adapters.commitments import CommitmentsMock
from pm.adapters.risk_log import RiskLogMock
from pm.adapters.teams import get_teams_reader
from pm.adapters.tracker import TrackerMock
from pm.seed.build import CHANNEL_ID
from pm.state.diff import FLAPPED, STATUS_CHANGED, compute_delta
from pm.state.snapshot import build_snapshot

MORNING = "2026-09-15T23:59:59+00:00"
END_OF_DAY = "2026-09-16T23:59:59+00:00"


def _snapshot(seeded_db_path, taken_at: str):
    tracker = TrackerMock(db_path=seeded_db_path)
    code_host = CodeHostMock(db_path=seeded_db_path)
    teams_reader = get_teams_reader(db_path=seeded_db_path)
    risk_log = RiskLogMock(db_path=seeded_db_path)
    commitments_store = CommitmentsMock(db_path=seeded_db_path)
    return build_snapshot(
        tracker,
        code_host,
        teams_reader,
        CHANNEL_ID,
        risk_log=risk_log,
        commitments_store=commitments_store,
        taken_at=taken_at,
    )


def test_delta_finds_exactly_the_three_golden_case_3_changes(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, MORNING)
    after = _snapshot(seeded_db_path, END_OF_DAY)

    delta = compute_delta(before, after, tracker)

    assert delta.before_taken_at == MORNING
    assert delta.after_taken_at == END_OF_DAY
    assert {item.item_id for item in delta.items} == {"PM-016", "PM-018", "PM-020"}


def test_pm016_appears_once_as_a_flap_not_a_status_change(seeded_db_path):
    """The row-level acceptance test's own wording: "The item that moved
    to done and back appears once in the computed delta, described
    accurately." Net status unchanged (in_progress both before and
    after), not silently dropped."""
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, MORNING)
    after = _snapshot(seeded_db_path, END_OF_DAY)

    delta = compute_delta(before, after, tracker)
    matches = [item for item in delta.items if item.item_id == "PM-016"]

    assert len(matches) == 1
    flap = matches[0]
    assert flap.kind == FLAPPED
    assert flap.before_status == "in_progress"
    assert flap.after_status == "in_progress"
    assert flap.transitions_in_window == 2
    assert "done" in flap.description
    assert "in_progress" in flap.description


def test_pm018_is_a_plain_status_change_despite_the_missing_assignee(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, MORNING)
    after = _snapshot(seeded_db_path, END_OF_DAY)

    delta = compute_delta(before, after, tracker)
    match = next(item for item in delta.items if item.item_id == "PM-018")

    assert match.kind == STATUS_CHANGED
    assert match.before_status == "in_progress"
    assert match.after_status == "in_review"


def test_pm020_moves_out_of_backlog_within_the_window(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, MORNING)
    after = _snapshot(seeded_db_path, END_OF_DAY)

    delta = compute_delta(before, after, tracker)
    match = next(item for item in delta.items if item.item_id == "PM-020")

    assert match.kind == STATUS_CHANGED
    assert match.before_status == "backlog"
    assert match.after_status == "in_progress"


def test_an_item_with_no_transitions_in_the_window_produces_no_delta(seeded_db_path):
    """PM-001 finished (in_progress -> done) back in August -- long before
    either boundary -- so both reconstructed statuses are "done" and it
    must not show up at all."""
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, MORNING)
    after = _snapshot(seeded_db_path, END_OF_DAY)

    delta = compute_delta(before, after, tracker)
    assert "PM-001" not in {item.item_id for item in delta.items}


def test_a_wider_window_around_the_same_flap_still_reports_it_once(seeded_db_path):
    """The flap is a property of the transition log within the window,
    not of exactly which two instants happen to bound it -- as long as
    the window's own edges still land on "in_progress" both times (here,
    after PM-016's initial 09-15 move out of backlog, and before any
    later change), widening it doesn't change the verdict."""
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, "2026-09-15T12:00:00+00:00")
    after = _snapshot(seeded_db_path, "2026-09-17T00:00:00+00:00")

    delta = compute_delta(before, after, tracker)
    matches = [item for item in delta.items if item.item_id == "PM-016"]
    assert len(matches) == 1
    assert matches[0].kind == FLAPPED


def test_a_window_predating_pm016s_first_move_is_a_real_status_change(seeded_db_path):
    """The other side of the same coin: widen the window enough to
    predate PM-016's very first transition (backlog -> in_progress on
    2026-09-15) and the net change is real, not a flap -- reconstruction
    from the first transition's own from_status, not an assumption baked into the test."""
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, "2026-09-10T00:00:00+00:00")
    after = _snapshot(seeded_db_path, "2026-09-20T00:00:00+00:00")

    delta = compute_delta(before, after, tracker)
    match = next(item for item in delta.items if item.item_id == "PM-016")
    assert match.kind == STATUS_CHANGED
    assert match.before_status == "backlog"
    assert match.after_status == "in_progress"


def test_an_empty_window_before_any_transitions_has_no_deltas(seeded_db_path):
    """Two snapshots taken back-to-back before anything in the dataset
    ever happened: nothing to report."""
    tracker = TrackerMock(db_path=seeded_db_path)
    before = _snapshot(seeded_db_path, "2026-08-01T00:00:00+00:00")
    after = _snapshot(seeded_db_path, "2026-08-01T00:00:01+00:00")

    delta = compute_delta(before, after, tracker)
    assert delta.items == []


# --- three accuracy gaps found by probing the engine with synthetic items ---


def _add_item(db_path, item_id, status, assignee, transitions):
    """A synthetic item with an explicit transition history, inserted
    straight into the seeded database before any snapshot is built."""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, item_id, status, "sprint-13", assignee, "2026-09-10"),
        )
        conn.executemany(
            "INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?)",
            [(item_id, *step) for step in transitions],
        )


def _delta_between_snapshots_with_a_change(db_path, change_sql=None, params=()):
    """Morning snapshot, then (optionally) one change to the tracker, then
    the end-of-day snapshot -- so the two snapshots really differ."""
    import sqlite3

    tracker = TrackerMock(db_path=db_path)
    before = _snapshot(db_path, MORNING)
    if change_sql:
        with sqlite3.connect(db_path) as conn:
            conn.execute(change_sql, params)
    after = _snapshot(db_path, END_OF_DAY)
    return {item.item_id: item for item in compute_delta(before, after, tracker).items}


def test_an_item_created_in_a_status_other_than_backlog_reports_its_real_starting_status(seeded_db_path):
    """The engine used to assume every item starts in backlog, so an item
    created directly in in_progress and finished in the window was reported
    as moving "from backlog to done"."""
    _add_item(seeded_db_path, "X-INITIAL", "done", "wei.chen", [("in_progress", "done", "2026-09-16T10:00:00")])

    item = _delta_between_snapshots_with_a_change(seeded_db_path)["X-INITIAL"]

    assert item.kind == STATUS_CHANGED
    assert (item.before_status, item.after_status) == ("in_progress", "done")
    assert item.description == "X-INITIAL moved from in_progress to done."


def test_a_status_change_that_is_also_a_reassignment_reports_both(seeded_db_path):
    """The reassignment used to be dropped whenever the status also changed."""
    _add_item(seeded_db_path, "X-BOTH", "in_progress", "wei.chen", [
        ("backlog", "in_progress", "2026-09-14"),
        ("in_progress", "done", "2026-09-16T10:00:00"),
    ])

    item = _delta_between_snapshots_with_a_change(
        seeded_db_path, "UPDATE items SET assignee_id = ?, status = 'done' WHERE id = ?", ("noah.becker", "X-BOTH")
    )["X-BOTH"]

    assert item.kind == STATUS_CHANGED
    assert (item.before_assignee, item.after_assignee) == ("wei.chen", "noah.becker")
    assert "moved from in_progress to done" in item.description
    assert "reassigned from 'wei.chen' to 'noah.becker'" in item.description


def test_a_flap_that_is_also_a_reassignment_reports_both_and_still_appears_once(seeded_db_path):
    import sqlite3

    with sqlite3.connect(seeded_db_path) as conn:
        original_owner = conn.execute("SELECT assignee_id FROM items WHERE id = 'PM-016'").fetchone()[0]
    new_owner = "noah.becker" if original_owner != "noah.becker" else "wei.chen"

    delta = _delta_between_snapshots_with_a_change(
        seeded_db_path, "UPDATE items SET assignee_id = ? WHERE id = ?", (new_owner, "PM-016")
    )

    assert [i for i in delta if i == "PM-016"] == ["PM-016"]
    item = delta["PM-016"]
    assert item.kind == FLAPPED
    assert (item.before_assignee, item.after_assignee) == (original_owner, new_owner)
    assert "churned (in_progress -> done -> in_progress)" in item.description
    assert f"reassigned from {original_owner!r} to {new_owner!r}" in item.description


def test_a_pure_reassignment_is_still_reported_as_one(seeded_db_path):
    delta = _delta_between_snapshots_with_a_change(
        seeded_db_path, "UPDATE items SET assignee_id = 'noah.becker' WHERE id = 'PM-001'"
    )

    item = delta["PM-001"]
    assert item.kind == "reassigned"
    assert item.after_assignee == "noah.becker"


def test_churn_that_ends_in_a_different_status_says_so_in_its_description(seeded_db_path):
    """in_progress -> done -> in_progress -> in_review used to be reported
    as a plain move from in_progress to in_review, with the bounce through
    done invisible in the description."""
    _add_item(seeded_db_path, "X-CHURN", "in_review", "wei.chen", [
        ("backlog", "in_progress", "2026-09-14"),
        ("in_progress", "done", "2026-09-16T09:00:00"),
        ("done", "in_progress", "2026-09-16T10:00:00"),
        ("in_progress", "in_review", "2026-09-16T11:00:00"),
    ])

    item = _delta_between_snapshots_with_a_change(seeded_db_path)["X-CHURN"]

    assert item.kind == STATUS_CHANGED
    assert (item.before_status, item.after_status) == ("in_progress", "in_review")
    assert item.transitions_in_window == 3
    assert "3 transitions" in item.description
    assert "in_progress -> done -> in_progress -> in_review" in item.description


def test_the_plain_single_move_descriptions_are_unchanged(seeded_db_path):
    """Guard: the fixes only add information where there was a gap."""
    delta = _delta_between_snapshots_with_a_change(seeded_db_path)

    assert delta["PM-018"].description == "PM-018 moved from in_progress to in_review."
    assert delta["PM-020"].description == "PM-020 moved from backlog to in_progress."
    assert delta["PM-016"].description == (
        "PM-016 churned (in_progress -> done -> in_progress) and ended back at in_progress -- "
        "net status unchanged, but it was not quiet."
    )
    assert delta["PM-018"].before_assignee is None and delta["PM-018"].after_assignee is None
