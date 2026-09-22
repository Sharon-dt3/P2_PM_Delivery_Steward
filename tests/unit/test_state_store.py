"""PM-05's own acceptance test, literally: "Two consecutive snapshots
persist and are independently readable." """

from __future__ import annotations

import pytest

from pm.adapters.code_host import CodeHostMock
from pm.adapters.commitments import CommitmentsMock
from pm.adapters.risk_log import RiskLogMock
from pm.adapters.teams import get_teams_reader
from pm.adapters.tracker import TrackerMock
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import ChannelSnapshot, ProjectSnapshot, build_snapshot
from pm.state.store import (
    DuplicateSnapshotError,
    SnapshotNotFoundError,
    list_snapshot_timestamps,
    read_snapshot,
    save_snapshot,
)


def _blank_snapshot(taken_at: str) -> ProjectSnapshot:
    return ProjectSnapshot(
        taken_at=taken_at,
        items=[],
        commits=[],
        channel=ChannelSnapshot(channel_id=CHANNEL_ID, messages=[]),
    )


def test_save_then_read_snapshot_round_trips(seeded_db_path):
    snapshot = _blank_snapshot("2026-09-18T00:00:00+00:00")
    save_snapshot(snapshot, db_path=seeded_db_path)

    read_back = read_snapshot("2026-09-18T00:00:00+00:00", db_path=seeded_db_path)
    assert read_back == snapshot


def test_save_snapshot_raises_for_a_duplicate_taken_at(seeded_db_path):
    snapshot = _blank_snapshot("2026-09-18T00:00:00+00:00")
    save_snapshot(snapshot, db_path=seeded_db_path)
    with pytest.raises(DuplicateSnapshotError):
        save_snapshot(snapshot, db_path=seeded_db_path)


def test_read_snapshot_raises_for_an_unknown_taken_at(seeded_db_path):
    with pytest.raises(SnapshotNotFoundError):
        read_snapshot("2099-01-01T00:00:00+00:00", db_path=seeded_db_path)


def test_list_snapshot_timestamps_returns_them_oldest_first(seeded_db_path):
    save_snapshot(_blank_snapshot("2026-09-18T10:05:00+00:00"), db_path=seeded_db_path)
    save_snapshot(_blank_snapshot("2026-09-18T10:00:00+00:00"), db_path=seeded_db_path)

    assert list_snapshot_timestamps(db_path=seeded_db_path) == [
        "2026-09-18T10:00:00+00:00",
        "2026-09-18T10:05:00+00:00",
    ]


def test_two_consecutive_snapshots_persist_and_are_independently_readable(seeded_db_path):
    """PM-05's own acceptance test, run end to end: build a real
    snapshot from the live tracker/code-host/channel adapters, change
    the underlying data, build a second real snapshot, persist both,
    and read each back independently by its own taken_at -- proving
    they are two distinct, intact records, not the same content saved
    twice."""
    tracker = TrackerMock(db_path=seeded_db_path)
    code_host = CodeHostMock(db_path=seeded_db_path)
    teams_reader = get_teams_reader(db_path=seeded_db_path)
    risk_log = RiskLogMock(db_path=seeded_db_path)
    commitments_store = CommitmentsMock(db_path=seeded_db_path)

    first = build_snapshot(
        tracker,
        code_host,
        teams_reader,
        CHANNEL_ID,
        risk_log=risk_log,
        commitments_store=commitments_store,
        taken_at="2026-09-18T10:00:00+00:00",
    )
    save_snapshot(first, db_path=seeded_db_path)

    # Change the underlying tracker state between snapshots, so the
    # second snapshot is demonstrably not just the first one persisted
    # again under a new timestamp.
    tracker.transition("PM-028", "blocked")

    second = build_snapshot(
        tracker,
        code_host,
        teams_reader,
        CHANNEL_ID,
        risk_log=risk_log,
        commitments_store=commitments_store,
        taken_at="2026-09-18T10:05:00+00:00",
    )
    save_snapshot(second, db_path=seeded_db_path)

    timestamps = list_snapshot_timestamps(db_path=seeded_db_path)
    assert timestamps == ["2026-09-18T10:00:00+00:00", "2026-09-18T10:05:00+00:00"]

    read_back_first = read_snapshot("2026-09-18T10:00:00+00:00", db_path=seeded_db_path)
    read_back_second = read_snapshot("2026-09-18T10:05:00+00:00", db_path=seeded_db_path)

    assert read_back_first.taken_at == "2026-09-18T10:00:00+00:00"
    assert read_back_second.taken_at == "2026-09-18T10:05:00+00:00"

    first_pm028 = next(item for item in read_back_first.items if item.id == "PM-028")
    second_pm028 = next(item for item in read_back_second.items if item.id == "PM-028")
    assert first_pm028.status == "in_progress"
    assert second_pm028.status == "blocked"
