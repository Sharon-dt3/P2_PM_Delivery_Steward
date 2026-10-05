"""A snapshot labelled with a past moment must show the project as it was
at that moment, not as it is today.

build_snapshot() used to read the tracker's CURRENT state and merely stamp
the requested taken_at on it, so a morning brief "as of 15 Sep" listed
PM-015 as blocked (it was blocked on 17 Sep), PM-018 and PM-027 as in
review and PM-020 as in progress -- facts from the future, in a brief that
also claimed it was day 9 of the sprint. These tests derive the correct
as-of state independently, straight from the raw rows, and compare.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pm.adapters.code_host import CodeHostMock
from pm.adapters.commitments import CommitmentsMock
from pm.adapters.risk_log import RiskLogMock
from pm.adapters.teams import get_teams_reader
from pm.adapters.tracker import TrackerMock
from pm.reporting.facts import compute_morning_brief_facts
from pm.seed.build import CANONICAL_STATUSES, CHANNEL_ID
from pm.state.snapshot import UNMAPPED, build_snapshot

MORNING = "2026-09-15T23:59:59+00:00"
END_OF_DAY = "2026-09-16T23:59:59+00:00"
FUTURE = "2026-12-01T00:00:00+00:00"


def _snapshot(db_path, taken_at):
    return build_snapshot(
        TrackerMock(db_path=db_path),
        CodeHostMock(db_path=db_path),
        get_teams_reader(db_path=db_path),
        CHANNEL_ID,
        risk_log=RiskLogMock(db_path=db_path),
        commitments_store=CommitmentsMock(db_path=db_path),
        taken_at=taken_at,
    )


def _when(value: str) -> datetime:
    parsed = datetime.fromisoformat(value if "T" in value else value + "T00:00:00")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _expected_status(conn, item_id: str, moment: datetime) -> str:
    """The status an item really had at `moment`, from the raw transition rows."""
    history = conn.execute(
        "SELECT from_status, to_status, changed_at FROM item_transitions WHERE item_id = ? ORDER BY id", (item_id,)
    ).fetchall()
    if not history:
        return conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    status = history[0][0]
    for _, to_status, changed_at in history:
        if _when(changed_at) <= moment:
            status = to_status
    return status


def test_a_past_snapshot_shows_every_item_as_it_actually_was(seeded_db_path):
    snapshot = _snapshot(seeded_db_path, MORNING)
    moment = _when(MORNING)

    with sqlite3.connect(seeded_db_path) as conn:
        for item in snapshot.items:
            raw = _expected_status(conn, item.id, moment)
            assert item.raw_status == raw, f"{item.id}: snapshot says {item.raw_status}, it was {raw}"
            assert item.status == (raw if raw in CANONICAL_STATUSES else UNMAPPED)

    by_id = {item.id: item for item in snapshot.items}
    assert by_id["PM-015"].status == "in_progress" and by_id["PM-015"].blocked_since is None  # blocked on the 17th
    assert by_id["PM-018"].status == "in_progress"  # reaches in_review on the 16th
    assert by_id["PM-020"].status == "backlog"  # moves to in_progress on the 16th
    assert by_id["PM-027"].status == "in_progress"  # in_review on the 17th


def test_the_end_of_day_snapshot_has_that_days_moves_but_not_later_ones(seeded_db_path):
    by_id = {item.id: item for item in _snapshot(seeded_db_path, END_OF_DAY).items}

    assert by_id["PM-018"].status == "in_review"
    assert by_id["PM-020"].status == "in_progress"
    assert by_id["PM-015"].status == "in_progress"  # still not blocked: that is the 17th


def test_a_snapshot_for_a_moment_after_everything_is_unchanged_current_state(seeded_db_path):
    """Guard: point-in-time rules must not alter snapshots of the present."""
    snapshot = _snapshot(seeded_db_path, FUTURE)
    current = {item.id: item for item in TrackerMock(db_path=seeded_db_path).list_items()}

    assert {item.id for item in snapshot.items} == set(current)
    for item in snapshot.items:
        assert item.raw_status == current[item.id].status
        assert item.blocked_since == current[item.id].blocked_since
    assert next(i for i in snapshot.items if i.id == "PM-015").blocked_since == "2026-09-17"


def test_items_created_after_the_snapshot_moment_are_absent(seeded_db_path):
    snapshot = _snapshot(seeded_db_path, "2026-09-11T00:00:00+00:00")
    ids = {item.id for item in snapshot.items}

    assert "PM-019" not in ids and "PM-020" not in ids  # created 12 and 15 Sep
    assert "PM-014" in ids  # created 8 Sep
    assert all(_when(item.created_at) <= _when(snapshot.taken_at) for item in snapshot.items)


def test_commits_and_commitments_after_the_snapshot_moment_are_left_out(seeded_db_path):
    moment_text = "2026-09-13T00:00:00+00:00"
    snapshot = _snapshot(seeded_db_path, moment_text)
    moment = _when(moment_text)

    with sqlite3.connect(seeded_db_path) as conn:
        all_commits = [r[0] for r in conn.execute("SELECT committed_at FROM commits")]
        all_commitments = [r[0] for r in conn.execute("SELECT made_at FROM commitments")]
    assert any(_when(t) > moment for t in all_commits) and any(_when(t) > moment for t in all_commitments)

    assert len(snapshot.commits) == sum(1 for t in all_commits if _when(t) <= moment)
    assert len(snapshot.commitments) == sum(1 for t in all_commitments if _when(t) <= moment)


def test_the_morning_brief_facts_for_a_past_moment_contain_nothing_from_later(seeded_db_path):
    facts = compute_morning_brief_facts(_snapshot(seeded_db_path, MORNING))

    blocked_today = {item.item_id for person in facts.people for item in person.blocked}
    assert "PM-015" not in blocked_today
    assert facts.sprint.day_number == 9
    assert [b.risk_id for b in facts.blockers] == ["RISK-002", "RISK-001"]
