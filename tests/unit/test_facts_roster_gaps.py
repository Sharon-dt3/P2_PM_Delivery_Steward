"""PM-10 "not omitted" also depends on who gets into facts.people. The roster
is the base set, and anyone seen in the data (item owner, commitment maker,
commit author) is unioned in, so a gap in the roster can add people but never
make known activity disappear. Each source of activity is probed separately
with a person who is NOT on the roster.
"""

from __future__ import annotations

from pm.adapters.code_host import Commit
from pm.adapters.commitments import Commitment
from pm.adapters.tracker import Assignee
from pm.reporting.facts import compute_morning_brief_facts
from pm.state.snapshot import ChannelSnapshot, NormalizedItem, ProjectSnapshot

ON_ROSTER = Assignee(id="on.roster", display_name="On Roster")


def _snapshot(*, roster=(ON_ROSTER,), items=(), commits=(), commitments=()):
    return ProjectSnapshot(
        taken_at="2026-09-16T23:59:59+00:00", items=list(items), commits=list(commits),
        channel=ChannelSnapshot(channel_id="c", messages=[]), commitments=list(commitments),
        roster=list(roster),
    )


def _people(snapshot):
    return {p.assignee_id: p for p in compute_morning_brief_facts(snapshot).people}


def test_a_roster_member_with_nothing_is_listed_as_inactive():
    people = _people(_snapshot())

    assert set(people) == {"on.roster"} and people["on.roster"].has_activity is False


def test_an_item_owner_missing_from_the_roster_is_not_dropped():
    item = NormalizedItem(id="PM-001", title="Ship login page", status="done", raw_status="done",
                          sprint_id="sprint-13", assignee_id="ghost", created_at="2026-09-10T00:00:00+00:00")

    people = _people(_snapshot(items=[item]))

    assert people["ghost"].has_activity and [i.item_id for i in people["ghost"].delivered] == ["PM-001"]


def test_a_commitment_maker_missing_from_the_roster_is_not_dropped():
    commitment = Commitment(id=1, member_id="ghost", text="I'll fix the bug", made_at="2026-09-10T00:00:00+00:00")

    people = _people(_snapshot(commitments=[commitment]))

    assert people["ghost"].has_activity and len(people["ghost"].committed) == 1


def test_a_commit_author_missing_from_the_roster_is_not_dropped():
    commit = Commit(sha="abc1234", author_id="ghost", message="chore: bump deps", committed_at="2026-09-15T10:00:00+00:00")

    people = _people(_snapshot(commits=[commit]))

    assert "ghost" in people
    assert people["ghost"].commit_count == 1 and people["ghost"].has_activity


def test_an_empty_roster_with_activity_still_lists_everyone_seen():
    commit = Commit(sha="abc1234", author_id="ghost", message="x", committed_at="2026-09-15T10:00:00+00:00")

    people = _people(_snapshot(roster=(), commits=[commit]))

    assert set(people) == {"ghost"}
