from __future__ import annotations

import pytest

from pm.adapters.tracker import (
    DuplicateItemError,
    ItemFilter,
    ItemNotFoundError,
    Tracker,
    TrackerItem,
    TrackerMock,
    TransitionRecord,
)


@pytest.fixture()
def tracker(seeded_db_path) -> Tracker:
    return TrackerMock(db_path=seeded_db_path)


def test_list_items_returns_every_seeded_item(tracker):
    assert len(tracker.list_items()) == 30


def test_list_items_filters_by_sprint(tracker):
    items = tracker.list_items(ItemFilter(sprint_id="sprint-12"))
    assert len(items) == 13
    assert all(item.sprint_id == "sprint-12" for item in items)


def test_list_items_filters_unassigned_only(tracker):
    """PM-04's own edge case: a missing assignee."""
    items = tracker.list_items(ItemFilter(unassigned_only=True))
    assert len(items) == 1
    assert items[0].id == "PM-018"


def test_list_items_filters_by_a_free_text_status(tracker):
    """PM-04's own edge case: a status outside the canonical enum
    filters the same as any other value."""
    items = tracker.list_items(ItemFilter(status="waiting_on_vendor"))
    assert len(items) == 1
    assert items[0].id == "PM-022"


def test_get_item_surfaces_the_stale_timestamp_edge_case(tracker):
    item = tracker.get_item("PM-014")
    assert item.status == "blocked"
    assert item.blocked_since == "2026-09-14"


def test_get_item_raises_for_unknown_id(tracker):
    with pytest.raises(ItemNotFoundError):
        tracker.get_item("PM-999")


def test_add_comment_persists_across_a_fresh_instance(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    comment = tracker.add_comment("PM-019", "Verified in staging.", tags=["verified"])
    assert comment.item_id == "PM-019"
    assert comment.tags == ["verified"]

    # a fresh instance, same db -- proves the comment table write
    # really persisted rather than being held in the mock's memory
    other_tracker = TrackerMock(db_path=seeded_db_path)
    assert other_tracker.get_item("PM-019").id == "PM-019"


def test_add_comment_raises_for_unknown_item(tracker):
    with pytest.raises(ItemNotFoundError):
        tracker.add_comment("PM-999", "no such item")


def test_create_item_then_get_item_round_trips(tracker):
    payload = TrackerItem(
        id="PM-031", title="New item", status="backlog", sprint_id="sprint-13", created_at="2026-09-18",
    )
    created = tracker.create_item(payload)
    assert created == payload
    assert tracker.get_item("PM-031") == payload


def test_create_item_raises_for_a_duplicate_id(tracker):
    """PM-04's own edge case: a duplicate item."""
    duplicate = TrackerItem(
        id="PM-001", title="Different title", status="backlog", sprint_id="sprint-12", created_at="2026-08-24",
    )
    with pytest.raises(DuplicateItemError):
        tracker.create_item(duplicate)


def test_transition_moves_status_and_is_visible_afterwards(tracker):
    updated = tracker.transition("PM-021", "in_progress")
    assert updated.status == "in_progress"
    assert tracker.get_item("PM-021").status == "in_progress"


def test_transition_to_blocked_sets_blocked_since(tracker):
    updated = tracker.transition("PM-028", "blocked")
    assert updated.status == "blocked"
    assert updated.blocked_since is not None


def test_transition_away_from_blocked_clears_blocked_since(tracker):
    updated = tracker.transition("PM-014", "in_progress")
    assert updated.blocked_since is None


def test_transition_raises_for_unknown_item(tracker):
    with pytest.raises(ItemNotFoundError):
        tracker.transition("PM-999", "done")


def test_list_transitions_returns_full_seeded_history_in_order(tracker):
    """PM-016's own planted difficulty (done, then reopened hours later,
    same calendar day): the full three-row history has to come back in
    the order it actually happened, not just the item's current status
    (which is back to in_progress -- net-unchanged from where it
    started)."""
    history = tracker.list_transitions("PM-016")
    assert [(t.from_status, t.to_status) for t in history] == [
        ("backlog", "in_progress"),
        ("in_progress", "done"),
        ("done", "in_progress"),
    ]
    assert all(isinstance(t, TransitionRecord) for t in history)
    assert history[1].changed_at == "2026-09-16T10:00:00"
    assert history[2].changed_at == "2026-09-16T15:30:00"


def test_list_transitions_is_empty_for_an_item_with_no_history(tracker):
    """PM-013: descoped straight out of backlog, no transitions at all --
    an empty list, not an error."""
    assert tracker.list_transitions("PM-013") == []


def test_list_transitions_raises_for_unknown_item(tracker):
    with pytest.raises(ItemNotFoundError):
        tracker.list_transitions("PM-999")


def test_list_transitions_reflects_a_transition_just_recorded(tracker):
    tracker.transition("PM-021", "in_progress")
    history = tracker.list_transitions("PM-021")
    assert len(history) == 1
    assert history[0].to_status == "in_progress"
