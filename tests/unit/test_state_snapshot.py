"""PM-05: normalising tracker/code-host/channel reads into one snapshot.
Free-text statuses must come through as UNMAPPED, never coerced -- see
pm/state/snapshot.py's own docstring."""

from __future__ import annotations

from pm.adapters.code_host import CodeHostMock
from pm.adapters.teams import get_teams_reader
from pm.adapters.tracker import TrackerMock
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import UNMAPPED, build_snapshot, normalize_item


def test_normalize_item_keeps_a_canonical_status_unchanged(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    item = tracker.get_item("PM-001")
    normalized = normalize_item(item)
    assert normalized.status == "done"
    assert normalized.raw_status == "done"


def test_normalize_item_marks_a_free_text_status_as_unmapped_but_keeps_it(seeded_db_path):
    """PM-03's own planted difficulty 8 (PM-022, "waiting_on_vendor") is
    exactly the live case this rule exists for."""
    tracker = TrackerMock(db_path=seeded_db_path)
    item = tracker.get_item("PM-022")
    normalized = normalize_item(item)
    assert normalized.status == UNMAPPED
    assert normalized.raw_status == "waiting_on_vendor"


def test_build_snapshot_normalizes_all_three_sources(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    code_host = CodeHostMock(db_path=seeded_db_path)
    teams_reader = get_teams_reader(db_path=seeded_db_path)

    snapshot = build_snapshot(
        tracker, code_host, teams_reader, CHANNEL_ID, taken_at="2026-09-18T00:00:00+00:00"
    )

    assert snapshot.taken_at == "2026-09-18T00:00:00+00:00"
    assert len(snapshot.items) == len(tracker.list_items())
    assert len(snapshot.commits) == len(code_host.list_commits())
    assert snapshot.channel.channel_id == CHANNEL_ID
    assert snapshot.channel.messages  # P1's real fixture data came through, not an empty stand-in

    unmapped = [item for item in snapshot.items if item.status == UNMAPPED]
    assert len(unmapped) == 1
    assert unmapped[0].id == "PM-022"
    assert unmapped[0].raw_status == "waiting_on_vendor"


def test_build_snapshot_defaults_taken_at_to_now_when_not_given(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    code_host = CodeHostMock(db_path=seeded_db_path)
    teams_reader = get_teams_reader(db_path=seeded_db_path)

    snapshot = build_snapshot(tracker, code_host, teams_reader, CHANNEL_ID)
    assert snapshot.taken_at  # non-empty
    assert "T" in snapshot.taken_at  # a real ISO 8601 timestamp, not a placeholder
