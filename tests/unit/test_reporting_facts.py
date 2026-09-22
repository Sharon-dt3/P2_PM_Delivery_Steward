"""PM-08: facts computed from a snapshot.

The row's own acceptance test, literal: "Regenerating on the same
snapshot yields identical facts; wording may differ." The "wording may
differ" half is proven in test_reporting_morning_brief.py (that's the
model's job); this file proves the "identical facts" half: everything
compute_morning_brief_facts() returns is a pure function of the snapshot
it's given.

ANCHOR_DATE is 2026-09-16 (see pm/seed/build.py's own docstring) --
squarely inside sprint-13 (2026-09-07 .. 2026-09-20), which is what
grounds every fact in this file against real, known seed values rather
than invented ones.
"""

from __future__ import annotations

from pm.adapters.code_host import CodeHostMock
from pm.adapters.commitments import CommitmentsMock
from pm.adapters.risk_log import RiskLogMock
from pm.adapters.teams import get_teams_reader
from pm.adapters.tracker import TrackerMock
from pm.reporting.facts import compute_morning_brief_facts
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import build_snapshot

ANCHOR_TAKEN_AT = "2026-09-16T23:59:59+00:00"


def _snapshot(seeded_db_path, taken_at: str = ANCHOR_TAKEN_AT):
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


def test_regenerating_on_the_same_snapshot_yields_identical_facts(seeded_db_path):
    """The row's own acceptance test, the "identical facts" half."""
    snapshot = _snapshot(seeded_db_path)
    first = compute_morning_brief_facts(snapshot)
    second = compute_morning_brief_facts(snapshot)
    assert first == second


def test_sprint_scope_resolves_to_sprint_13_on_the_anchor_date(seeded_db_path):
    snapshot = _snapshot(seeded_db_path)
    facts = compute_morning_brief_facts(snapshot)

    assert facts.sprint is not None
    assert facts.sprint.sprint_id == "sprint-13"
    assert facts.sprint.start_date == "2026-09-07"
    assert facts.sprint.end_date == "2026-09-20"
    # 2026-09-16 is the 10th day of a sprint starting 2026-09-07 (inclusive).
    assert facts.sprint.day_number == 10
    # 2026-09-07 .. 2026-09-20 inclusive is 14 days.
    assert facts.sprint.total_days == 14


def test_sprint_scope_is_none_outside_every_known_sprint(seeded_db_path):
    """An honest gap, not an error or a fallback guess."""
    snapshot = _snapshot(seeded_db_path, taken_at="2026-01-01T00:00:00+00:00")
    facts = compute_morning_brief_facts(snapshot)
    assert facts.sprint is None


def test_blocked_item_lands_in_the_blocked_bucket_not_pending(seeded_db_path):
    """PM-014, olivia.dupree's own stale blocker (PM-03 difficulty 1)."""
    snapshot = _snapshot(seeded_db_path)
    facts = compute_morning_brief_facts(snapshot)
    olivia = next(person for person in facts.people if person.assignee_id == "olivia.dupree")

    assert {item.item_id for item in olivia.blocked} >= {"PM-014"}
    assert "PM-014" not in {item.item_id for item in olivia.pending}
    assert "PM-014" not in {item.item_id for item in olivia.delivered}


def test_done_item_lands_in_the_delivered_bucket(seeded_db_path):
    snapshot = _snapshot(seeded_db_path)
    facts = compute_morning_brief_facts(snapshot)
    # PM-001 is done, assigned per the seed -- find whoever owns it via the snapshot itself.
    pm001 = next(item for item in snapshot.items if item.id == "PM-001")
    assert pm001.assignee_id is not None
    owner = next(person for person in facts.people if person.assignee_id == pm001.assignee_id)
    assert "PM-001" in {item.item_id for item in owner.delivered}


def test_committed_bucket_comes_from_commitments_not_tracker_status(seeded_db_path):
    """wei.chen made two seeded commitments (PM-028, PM-023) -- this
    bucket must reflect that regardless of either item's own tracker
    status (PM-023 is blocked; PM-028 is not)."""
    snapshot = _snapshot(seeded_db_path)
    facts = compute_morning_brief_facts(snapshot)
    wei = next(person for person in facts.people if person.assignee_id == "wei.chen")
    assert {c.item_id for c in wei.committed} == {"PM-028", "PM-023"}


def test_blockers_are_ranked_high_severity_first(seeded_db_path):
    """RISK-002 (high, PM-024) must outrank RISK-001 (medium, PM-023).
    RISK-003 is mitigated, not open, and must not appear at all."""
    snapshot = _snapshot(seeded_db_path)
    facts = compute_morning_brief_facts(snapshot)

    assert [blocker.risk_id for blocker in facts.blockers] == ["RISK-002", "RISK-001"]
    assert facts.blockers[0].severity == "high"
    assert facts.blockers[1].severity == "medium"
    assert "RISK-003" not in {blocker.risk_id for blocker in facts.blockers}


def test_blocker_facts_resolve_the_related_items_own_assignee(seeded_db_path):
    snapshot = _snapshot(seeded_db_path)
    facts = compute_morning_brief_facts(snapshot)
    risk_002 = next(blocker for blocker in facts.blockers if blocker.risk_id == "RISK-002")
    assert risk_002.related_item_id == "PM-024"
    assert risk_002.assignee_id == "olivia.dupont"
