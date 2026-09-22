"""PM-05: normalising tracker/code-host/channel reads into one snapshot.
Free-text statuses must come through as UNMAPPED, never coerced -- see
pm/state/snapshot.py's own docstring."""

from __future__ import annotations

import pytest

from pm.adapters.code_host import CodeHostMock
from pm.adapters.commitments import CommitmentsMock
from pm.adapters.risk_log import RiskLogMock
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


def test_build_snapshot_normalizes_all_five_sources(seeded_db_path):
    """PM-08 extended this from three sources to five (risk log,
    commitments) so its own morning-brief facts can come entirely from
    one ProjectSnapshot."""
    tracker = TrackerMock(db_path=seeded_db_path)
    code_host = CodeHostMock(db_path=seeded_db_path)
    teams_reader = get_teams_reader(db_path=seeded_db_path)
    risk_log = RiskLogMock(db_path=seeded_db_path)
    commitments_store = CommitmentsMock(db_path=seeded_db_path)

    snapshot = build_snapshot(
        tracker,
        code_host,
        teams_reader,
        CHANNEL_ID,
        risk_log=risk_log,
        commitments_store=commitments_store,
        taken_at="2026-09-18T00:00:00+00:00",
    )

    assert snapshot.taken_at == "2026-09-18T00:00:00+00:00"
    assert len(snapshot.items) == len(tracker.list_items())
    assert len(snapshot.commits) == len(code_host.list_commits())
    assert snapshot.channel.channel_id == CHANNEL_ID
    assert snapshot.channel.messages  # P1's real fixture data came through, not an empty stand-in
    assert len(snapshot.sprints) == len(tracker.list_sprints())
    assert len(snapshot.commitments) == len(commitments_store.list_commitments())
    assert len(snapshot.risks) == len(risk_log.list_risks())

    unmapped = [item for item in snapshot.items if item.status == UNMAPPED]
    assert len(unmapped) == 1
    assert unmapped[0].id == "PM-022"
    assert unmapped[0].raw_status == "waiting_on_vendor"


def test_build_snapshot_defaults_taken_at_to_now_when_not_given(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    code_host = CodeHostMock(db_path=seeded_db_path)
    teams_reader = get_teams_reader(db_path=seeded_db_path)
    risk_log = RiskLogMock(db_path=seeded_db_path)
    commitments_store = CommitmentsMock(db_path=seeded_db_path)

    snapshot = build_snapshot(
        tracker, code_host, teams_reader, CHANNEL_ID, risk_log=risk_log, commitments_store=commitments_store
    )
    assert snapshot.taken_at  # non-empty
    assert "T" in snapshot.taken_at  # a real ISO 8601 timestamp, not a placeholder


def test_build_snapshot_requires_the_new_keyword_only_adapters(seeded_db_path):
    """risk_log/commitments_store are required, not optional -- PM-08's
    own addition to PM-05's build_snapshot() signature. Every call site
    must be explicit about what it's reading from, the same posture
    tracker/code_host/teams_reader already take."""
    tracker = TrackerMock(db_path=seeded_db_path)
    code_host = CodeHostMock(db_path=seeded_db_path)
    teams_reader = get_teams_reader(db_path=seeded_db_path)

    with pytest.raises(TypeError):
        build_snapshot(tracker, code_host, teams_reader, CHANNEL_ID)
