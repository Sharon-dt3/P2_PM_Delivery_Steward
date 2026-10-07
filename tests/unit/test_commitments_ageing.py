"""PM-24: the ageing view of all open commitments, checked against the seed's own eight, by hand on 18 Sep 2026.

#8 noah  due 14 Sep  4 days overdue      #5 wei    due 15 Sep  3 days overdue
#6 dupree due 16 Sep 2 days overdue      #7 dupont due 17 Sep  1 day overdue
#4 noah  due 18 Sep  today               #1 mateo  "end of week", said Tue 15 Sep, so Fri 18 Sep: today
#3 wei   due 19 Sep  in 1 day            #2 dupont due 25 Sep  in 7 days
"""

from __future__ import annotations

from datetime import date

import pytest

from pm.adapters.tracker import TrackerMock
from pm.commitments.ageing import BUCKETS, ageing_view, effective_due, format_ageing
from pm.commitments.store import (
    CANCELLED,
    ESCALATED,
    FULFILLED,
    NUDGED,
    CommitmentTracker,
)

TODAY = date(2026, 9, 18)


@pytest.fixture()
def world(seeded_db_path):
    tracker, tracker_items = CommitmentTracker(seeded_db_path), TrackerMock(db_path=seeded_db_path)
    names = {a.id: a.display_name for a in tracker_items.list_assignees()}
    status = lambda item_id: tracker_items.get_item(item_id).status
    return tracker, status, names


def _rows(world, today=TODAY):
    tracker, status, names = world
    return ageing_view(tracker=tracker, today=today, item_status=status, names=names)


def test_every_open_commitment_is_listed_with_how_overdue_it_is(world):
    rows = {r.commitment.id: r for r in _rows(world)}

    assert len(rows) == 8
    assert {i: r.overdue_days for i, r in rows.items() if r.overdue_days} == {8: 4, 5: 3, 6: 2, 7: 1}
    assert {i: r.days_to_due for i, r in rows.items() if r.days_to_due is not None} == {4: 0, 1: 0, 3: 1, 2: 7}


def test_each_commitment_is_in_the_bucket_a_person_would_expect(world):
    buckets = {r.commitment.id: r.bucket for r in _rows(world)}

    assert buckets == {8: "overdue 3 to 7 days", 5: "overdue 3 to 7 days", 6: "overdue 1 to 2 days", 7: "overdue 1 to 2 days",
                       4: "due today", 1: "due today", 3: "due in 1 to 3 days", 2: "due later"}


def test_most_overdue_first(world):
    assert [r.commitment.id for r in _rows(world)] == [8, 5, 6, 7, 1, 4, 3, 2]


def test_how_long_each_has_been_open(world):
    rows = {r.commitment.id: r for r in _rows(world)}

    assert rows[8].open_days == 10 and rows[6].open_days == 8 and rows[1].open_days == 3  # made 8, 10 and 15 Sep


def test_a_relative_due_date_in_the_seed_is_read_as_a_day(world):
    tracker, _, _ = world

    assert effective_due(tracker.get(1)) == "2026-09-18"  # "end of week", made on Tuesday 15 Sep


def test_the_owner_is_a_name_and_the_item_status_is_shown(world):
    rows = {r.commitment.id: r for r in _rows(world)}

    assert rows[6].owner == "Olivia Dupree" and rows[6].item_status == "blocked" and rows[4].item_status == "in_progress"


def test_a_commitment_whose_item_is_done_is_not_open(world):
    tracker, _, names = world
    rows = ageing_view(tracker=tracker, today=TODAY, names=names, item_status=lambda i: "done" if i == "PM-014" else "blocked")

    assert 6 not in {r.commitment.id for r in rows} and len(rows) == 7  # the tracker is the truth about the work


def test_closed_commitments_are_not_listed(world):
    tracker, _, _ = world
    tracker.close(8, status=FULFILLED, day="2026-09-17")
    tracker.close(5, status=CANCELLED, day="2026-09-17")

    assert {r.commitment.id for r in _rows(world)} == {6, 7, 4, 1, 3, 2}


def test_a_commitment_with_no_date_is_listed_in_its_own_bucket(world):
    tracker, _, _ = world
    tracker.add(member_id="wei.chen", text="I'll get to the retry queue when I can.", made_at="2026-09-10", source_message_id="m-x")

    undated = [r for r in _rows(world) if r.bucket == "no due date"]

    assert len(undated) == 1 and undated[0].due is None and undated[0].overdue_days is None and undated[0].open_days == 8
    assert _rows(world)[-1].bucket == "no due date"  # and it sorts last


def test_what_has_been_done_about_each_is_shown(world):
    tracker, _, _ = world
    tracker.record_event(8, NUDGED, "2026-09-13")
    tracker.record_event(5, ESCALATED, "2026-09-18")

    rows = {r.commitment.id: r for r in _rows(world)}

    assert rows[8].nudged and not rows[8].escalated and rows[5].escalated and not rows[6].nudged


def test_the_view_moves_with_the_day(world):
    """A week later every commitment is overdue and #8 has crossed the seven-day line."""
    rows = {r.commitment.id: r for r in _rows(world, date(2026, 9, 26))}

    assert all(r.overdue_days for r in rows.values()) and rows[8].bucket == "overdue more than 7 days" and rows[8].overdue_days == 12


def test_the_printed_view_groups_by_bucket_and_names_people(world):
    text = format_ageing(_rows(world), TODAY)

    assert text.startswith("Open commitments as of 2026-09-18: 8")
    for bucket in ("overdue 3 to 7 days", "overdue 1 to 2 days", "due today", "due in 1 to 3 days", "due later"):
        assert bucket in text
    assert "Olivia Dupree" in text and "overdue 4d" in text and "[PM-014: blocked]" in text and text.index("overdue 3 to 7") < text.index("due later")


def test_an_empty_view_says_so(world):
    tracker, _, _ = world
    for c in tracker.list():
        tracker.close(c.id, status=CANCELLED, day="2026-09-18")

    assert format_ageing(_rows(world), TODAY).endswith("none.")


def test_the_buckets_are_the_ones_the_view_can_produce(world):
    assert {r.bucket for r in _rows(world, date(2026, 9, 26))} <= set(BUCKETS)
