"""PM-16, the gap set: which current blockers have NO entry in the risk log.

This half is pure Python -- no model. A blocker is an item whose tracker status is
blocked at the snapshot's moment; it is covered if ANY risk-log entry (in any status)
names that item; the gap set is the blockers nobody has logged. Everything numeric in
the evidence (days blocked, days left in the sprint, days a commitment is overdue) is
computed here, from dates in the data, never by a model.
"""

from __future__ import annotations

from pm.adapters.risk_log import Risk
from pm.adapters.tracker import Assignee
from pm.risk.gaps import covered_items, current_blockers, find_gaps
from pm.seed.build import ANCHOR_DATE
from pm.state.snapshot import (
    ChannelSnapshot,
    NormalizedItem,
    ProjectSnapshot,
    build_current_snapshot,
)

AS_OF = f"{ANCHOR_DATE.isoformat()}T12:00:00+00:00"  # 2026-09-18


def _seeded(seeded_db_path, taken_at=AS_OF):
    return build_current_snapshot(seeded_db_path, taken_at=taken_at, tz_name="Asia/Colombo")


def _item(item_id, status="blocked", assignee="aisha.rahman", blocked_since="2026-09-14", title="Some blocked work"):
    return NormalizedItem(
        id=item_id, title=title, status=status, raw_status=status, sprint_id="sprint-13", assignee_id=assignee,
        created_at="2026-09-10T00:00:00+00:00", blocked_since=blocked_since,
    )


def _snapshot(items, risks=(), roster=(("aisha.rahman", "Aisha Rahman"),), taken_at="2026-09-18T12:00:00+00:00"):
    from pm.adapters.tracker import Sprint

    return ProjectSnapshot(
        taken_at=taken_at, items=list(items), commits=[], channel=ChannelSnapshot(channel_id="c", messages=[]),
        sprints=[Sprint(id="sprint-13", display_name="Sprint 13", start_date="2026-09-07", end_date="2026-09-20")],
        risks=list(risks), roster=[Assignee(id=i, display_name=n) for i, n in roster], timezone="UTC",
    )


def _risk(risk_id, item, status="open"):
    return Risk(id=risk_id, title="t", description="d", severity="low", status=status, related_item_id=item, opened_at="2026-09-10")


# --- the gap set on the seeded project --------------------------------------------------------------------


def test_the_seeded_project_has_four_current_blockers_two_of_them_in_the_risk_log(seeded_db_path):
    snapshot = _seeded(seeded_db_path)

    assert current_blockers(snapshot) == ["PM-014", "PM-015", "PM-023", "PM-024"]
    assert covered_items(snapshot) == {"PM-023", "PM-024"}  # RISK-001 and RISK-002; RISK-003 names no item


def test_the_gap_set_is_exactly_the_two_blockers_nobody_has_logged(seeded_db_path):
    assert [g.item_id for g in find_gaps(_seeded(seeded_db_path))] == ["PM-014", "PM-015"]


def test_it_is_a_point_in_time_question(seeded_db_path):
    """PM-015 only became blocked on 17 Sep: a day earlier it is not a blocker yet."""
    earlier = _seeded(seeded_db_path, "2026-09-16T12:00:00+00:00")

    assert [g.item_id for g in find_gaps(earlier)] == ["PM-014"]


def test_a_risk_in_any_status_covers_its_blocker():
    snapshot = _snapshot([_item("PM-001"), _item("PM-002"), _item("PM-003")],
                         risks=[_risk("RISK-001", "PM-001", "open"), _risk("RISK-002", "PM-002", "mitigated")])

    assert [g.item_id for g in find_gaps(snapshot)] == ["PM-003"]  # a closed or mitigated entry is still an entry


def test_a_risk_that_names_no_item_covers_nothing():
    snapshot = _snapshot([_item("PM-001")], risks=[_risk("RISK-003", None, "mitigated")])

    assert [g.item_id for g in find_gaps(snapshot)] == ["PM-001"]


def test_only_blocked_items_are_blockers():
    items = [_item("PM-001", status="done"), _item("PM-002", status="in_progress"), _item("PM-003", status="UNMAPPED"),
             _item("PM-004", status="backlog"), _item("PM-005", status="blocked")]

    assert [g.item_id for g in find_gaps(_snapshot(items))] == ["PM-005"]


def test_the_free_text_waiting_on_vendor_item_is_not_a_blocker(seeded_db_path):
    """PM-022's status is outside the canonical set; it is pending in the brief and
    is not claimed as a blocker here."""
    assert "PM-022" not in [g.item_id for g in find_gaps(_seeded(seeded_db_path))]


def test_no_blockers_and_a_fully_covered_log_give_no_gaps():
    assert find_gaps(_snapshot([_item("PM-001", status="done")])) == []
    assert find_gaps(_snapshot([_item("PM-001")], risks=[_risk("RISK-001", "PM-001")])) == []


def test_it_is_deterministic_and_sorted(seeded_db_path):
    snapshot = _seeded(seeded_db_path)

    assert find_gaps(snapshot) == find_gaps(snapshot)
    ids = [g.item_id for g in find_gaps(_snapshot([_item("PM-009"), _item("PM-002"), _item("PM-005")]))]
    assert ids == ["PM-002", "PM-005", "PM-009"]


# --- the arithmetic is code's ------------------------------------------------------------------------------


def test_days_blocked_and_sprint_days_left_are_computed_from_the_dates(seeded_db_path):
    pm014, pm015 = find_gaps(_seeded(seeded_db_path))

    assert (pm014.blocked_since, pm014.days_blocked) == ("2026-09-14", 4)  # 14 Sep to 18 Sep
    assert (pm015.blocked_since, pm015.days_blocked) == ("2026-09-17", 1)
    assert pm014.sprint_end == pm015.sprint_end == "2026-09-20" and pm014.sprint_days_left == 2
    assert pm014.as_of == "2026-09-18"


def test_a_commitment_overdue_is_counted_in_days(seeded_db_path):
    pm014, pm015 = find_gaps(_seeded(seeded_db_path))

    (commitment,) = pm014.commitments
    assert commitment.due_date_iso == "2026-09-16" and commitment.days_overdue == 2
    assert pm015.commitments == ()  # nobody committed anything on PM-015


def test_a_commitment_not_yet_due_is_not_overdue():
    from pm.adapters.commitments import Commitment

    snapshot = _snapshot([_item("PM-001")])
    snapshot = snapshot.model_copy(update={"commitments": [
        Commitment(id=1, member_id="aisha.rahman", item_id="PM-001", text="Will unblock by Friday", due_date_iso="2026-09-25",
                   made_at="2026-09-12")]})

    (commitment,) = find_gaps(snapshot)[0].commitments

    assert commitment.days_overdue is None and commitment.days_until_due == 7


def test_a_blocker_with_no_blocked_since_date_gets_no_invented_duration():
    gap = find_gaps(_snapshot([_item("PM-001", blocked_since=None)]))[0]

    assert gap.blocked_since is None and gap.days_blocked is None
    assert "days as of" not in gap.evidence_text()


def test_the_local_date_not_the_utc_date_is_used():
    """Blocked on 14 Sep; at 02:00 UTC on 18 Sep it is already the 18th in Colombo and
    still the 18th in UTC, but at 22:00 UTC on 17 Sep it is the 18th in Colombo."""
    snapshot = _snapshot([_item("PM-001")], taken_at="2026-09-17T22:00:00+00:00").model_copy(update={"timezone": "Asia/Colombo"})

    assert find_gaps(snapshot)[0].as_of == "2026-09-18"


# --- the suggested owner: only where evidenced ----------------------------------------------------------------


def test_the_owner_is_the_trackers_assignee_with_the_evidence_named(seeded_db_path):
    pm014, pm015 = find_gaps(_seeded(seeded_db_path))

    assert (pm014.owner.id, pm014.owner.name) == ("olivia.dupree", "Olivia Dupree")
    assert "assignee of PM-014" in pm014.owner.evidence
    assert (pm015.owner.id, pm015.owner.name) == ("wei.chen", "Wei Chen")


def test_no_assignee_means_no_suggested_owner():
    gap = find_gaps(_snapshot([_item("PM-001", assignee=None)]))[0]

    assert gap.owner is None


def test_an_assignee_who_is_not_on_the_roster_is_still_evidenced_by_id():
    gap = find_gaps(_snapshot([_item("PM-001", assignee="ghost.user")]))[0]

    assert (gap.owner.id, gap.owner.name) == ("ghost.user", "ghost.user")


def test_the_owner_is_never_taken_from_anywhere_but_the_tracker(seeded_db_path):
    """A commitment by someone else, a commit author or a message author does not make
    them the suggested owner: only the item's assignee is evidence of ownership."""
    pm014 = find_gaps(_seeded(seeded_db_path))[0]

    assert pm014.owner.id == "olivia.dupree" and pm014.commitments[0].member_id == "olivia.dupree"
    gap = find_gaps(_snapshot([_item("PM-001", assignee=None)]).model_copy(update={"commitments": []}))[0]
    assert gap.owner is None


# --- the evidence text (what the model is shown and must stay inside) ----------------------------------------------


def test_the_evidence_text_carries_every_fact_with_its_reference(seeded_db_path):
    pm014 = find_gaps(_seeded(seeded_db_path))[0]
    text = pm014.evidence_text()

    assert pm014.reference == "item:PM-014"
    for fragment in (
        "PM-014", "Search index blocked on staging DB migration", "Olivia Dupree", "olivia.dupree",
        "2026-09-14", "4 days", "2026-09-18", "Sprint 13", "2026-09-20", "2 days left",
        "Expect the staging DB migration to unblock the search index work by 2026-09-16.", "2 days overdue",
        "c000018", "PM-014: attempt search index workaround pending migration",
        "proj-gamma-0192", "the search index is blocked, the staging DB migration hasn't run yet.",
    ):
        assert fragment in text, fragment


def test_the_evidence_text_for_the_second_blocker_has_no_commitment_line(seeded_db_path):
    text = find_gaps(_seeded(seeded_db_path))[1].evidence_text()

    assert "PM-015" in text and "wei.chen" in text and "1 day" in text and "Commitment" not in text
    assert "Should the export job handle the null case, or is that out of scope?" in text
