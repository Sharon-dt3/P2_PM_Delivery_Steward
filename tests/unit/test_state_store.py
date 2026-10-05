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


FIRST_AT = "2026-09-18T10:00:00+00:00"
SECOND_AT = "2026-09-18T10:05:00+00:00"


def _two_consecutive_snapshots(db_path):
    """Two real snapshots five minutes apart, with a genuine tracker change
    between them, built and returned but not yet saved."""
    tracker = TrackerMock(db_path=db_path)
    adapters = {
        "risk_log": RiskLogMock(db_path=db_path),
        "commitments_store": CommitmentsMock(db_path=db_path),
    }
    code_host = CodeHostMock(db_path=db_path)
    reader = get_teams_reader(db_path=db_path)

    first = build_snapshot(tracker, code_host, reader, CHANNEL_ID, taken_at=FIRST_AT, **adapters)
    tracker.transition("PM-028", "blocked")
    second = build_snapshot(tracker, code_host, reader, CHANNEL_ID, taken_at=SECOND_AT, **adapters)
    return first, second


def test_each_snapshot_reads_back_complete_not_just_its_timestamp(seeded_db_path):
    """The acceptance test above checks the timestamp and one item. This
    requires the WHOLE snapshot (items, commits, channel messages, sprints,
    commitments, risks, roster) to survive the round trip intact, for both."""
    first, second = _two_consecutive_snapshots(seeded_db_path)
    save_snapshot(first, db_path=seeded_db_path)
    save_snapshot(second, db_path=seeded_db_path)

    assert read_snapshot(FIRST_AT, db_path=seeded_db_path) == first
    assert read_snapshot(SECOND_AT, db_path=seeded_db_path) == second
    assert first != second


def test_a_snapshot_stands_alone_and_is_not_stored_relative_to_the_other(seeded_db_path):
    """Independently readable means neither row needs the other. Remove the
    first snapshot entirely: the second must still read back complete, and
    removing the second must leave the first untouched."""
    import sqlite3

    first, second = _two_consecutive_snapshots(seeded_db_path)
    save_snapshot(first, db_path=seeded_db_path)
    save_snapshot(second, db_path=seeded_db_path)

    with sqlite3.connect(seeded_db_path) as conn:
        conn.execute("DELETE FROM snapshots WHERE taken_at = ?", (FIRST_AT,))
    assert list_snapshot_timestamps(db_path=seeded_db_path) == [SECOND_AT]
    assert read_snapshot(SECOND_AT, db_path=seeded_db_path) == second

    save_snapshot(first, db_path=seeded_db_path)
    with sqlite3.connect(seeded_db_path) as conn:
        conn.execute("DELETE FROM snapshots WHERE taken_at = ?", (SECOND_AT,))
    assert read_snapshot(FIRST_AT, db_path=seeded_db_path) == first


def test_snapshots_persist_across_separate_processes(seeded_db_path):
    """Persisted means on disk, not held in this process's memory: one
    process writes both snapshots and exits, a different process reads
    them back, and what it reads equals what was written."""
    import os
    import subprocess
    import sys

    first, second = _two_consecutive_snapshots(seeded_db_path)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    writer = (
        "import sys, json\n"
        "from pm.state.snapshot import ProjectSnapshot\n"
        "from pm.state.store import save_snapshot\n"
        "for payload in json.load(sys.stdin):\n"
        "    save_snapshot(ProjectSnapshot.model_validate_json(payload), db_path=sys.argv[1])\n"
    )
    reader = (
        "import sys, json\n"
        "from pm.state.store import read_snapshot\n"
        "print(json.dumps([read_snapshot(at, db_path=sys.argv[1]).model_dump_json() for at in sys.argv[2:]]))\n"
    )
    import json

    subprocess.run(
        [sys.executable, "-c", writer, str(seeded_db_path)],
        input=json.dumps([first.model_dump_json(), second.model_dump_json()]),
        text=True, env=env, check=True,
    )
    out = subprocess.run(
        [sys.executable, "-c", reader, str(seeded_db_path), FIRST_AT, SECOND_AT],
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    read_first, read_second = (ProjectSnapshot.model_validate_json(p) for p in json.loads(out))

    assert read_first == first
    assert read_second == second
