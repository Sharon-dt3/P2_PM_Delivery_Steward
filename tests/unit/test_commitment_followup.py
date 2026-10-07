"""PM-24: nudge before the due date, an overdue record after it, escalation to the lead beyond the threshold, and
the shared per-person per-day cap, on the seed's eight commitments.

Friday 18 Sep 2026 (a working day, 09:00 in Colombo). By hand, the seed's commitments are:
  #8 noah   PM-021 due 14 Sep (4 days overdue)   #5 wei    PM-023 due 15 Sep (3 overdue)   #6 dupree PM-014 due 16 Sep (2 overdue)
  #7 dupont PM-024 due 17 Sep (1 overdue)        #4 noah   PM-019 due 18 Sep (today)       #1 mateo  PM-016 "end of week" = 18 Sep
  #3 wei    PM-028 due 19 Sep (tomorrow)         #2 dupont PM-022 due 25 Sep (a week away)
The lead is noah.becker; the escalation threshold is 3 days; the shared cap is 1 reminder a person a day.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import PENDING, REJECTED, ProposalStore

from pm.adapters.tracker import TrackerMock
from pm.approval import service
from pm.approval.audit import AUTO_APPROVER, audit_trail
from pm.approval.service import ApprovalPolicy
from pm.commitments import followup as fu
from pm.commitments.cap import SharedNudgeCap
from pm.commitments.followup import (
    FollowupSettings,
    load_followup_settings,
    run_followups,
)
from pm.commitments.store import (
    ESCALATED,
    FULFILLED,
    NUDGED,
    OVERDUE,
    CommitmentTracker,
)

FRI = datetime(2026, 9, 18, 3, 30, tzinfo=timezone.utc)  # 09:00 Colombo
SAT = datetime(2026, 9, 19, 3, 30, tzinfo=timezone.utc)
MON = datetime(2026, 9, 21, 3, 30, tzinfo=timezone.utc)
TUE = datetime(2026, 9, 22, 3, 30, tzinfo=timezone.utc)
APPROVER = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))

SETTINGS = FollowupSettings(
    channel_id="19:proj-gamma@thread.tacv2", timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], non_working_dates=[],
    nudge_days_before_due=1, escalation_threshold_days=3, lead_id="noah.becker", exceptions=frozenset(), cap_per_person_per_day=1,
)


class World:
    def __init__(self, db, tmp_path):
        self.db, self.tmp = db, tmp_path
        self.log = LogPublisher(tmp_path / "out.jsonl")
        self.tracker, self.store = CommitmentTracker(db), ProposalStore(db)
        self.items = TrackerMock(db_path=db)
        self.names = {a.id: a.display_name for a in self.items.list_assignees()}
        self.p1 = tmp_path / "p1.db"
        self.p1_ledger()

    def p1_ledger(self, rows=()):
        if self.p1.exists():
            self.p1.unlink()
        conn = sqlite3.connect(self.p1)
        conn.execute("CREATE TABLE nudges (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL, member_id TEXT NOT NULL, "
                     "date TEXT NOT NULL, proposal_id TEXT, sent_at TEXT, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT)")
        for i, (member, day, sent) in enumerate(rows):
            conn.execute("INSERT INTO nudges (channel_id, member_id, date, sent_at, idempotency_key) VALUES ('19:p1@thread.tacv2', ?, ?, ?, ?)",
                         (member, day, sent, f"k{i}"))
        conn.commit()
        conn.close()

    def status(self, item_id):
        from pm.adapters.tracker import ItemNotFoundError

        try:
            return self.items.get_item(item_id).status
        except ItemNotFoundError:
            return None

    def run(self, moment=FRI, settings=SETTINGS, dry_run=False, policy=None, **kw):
        return run_followups(moment=moment, settings=settings, publisher=self.log, db_path=self.db, policy=policy or APPROVER,
                             item_status=self.status, names=self.names, dry_run=dry_run, **kw)

    def reminded_before(self, *members):
        """These people have been reminded by this agent before (so their next reminder needs no first-time approval)."""
        cap = SharedNudgeCap(db_path=self.db, cap=1)
        for m in members:
            cap.record(member_id=m, day="2026-09-10", commitment_id=None, proposal_id=None, key=f"old:{m}")
            cap.mark_sent(f"old:{m}", sent_at="2026-09-10T03:00:00+00:00", day="2026-09-10")

    def by_commitment(self, results, cid, action=None):
        return [r for r in results if r.commitment_id == cid and (action is None or r.action == action)]

    def sent(self):
        return self.log.read_log()


@pytest.fixture()
def w(seeded_db_path, tmp_path, monkeypatch):
    world = World(seeded_db_path, tmp_path)
    monkeypatch.setenv("P1_DB_PATH", str(world.p1))  # the one place P1's ledger is found, for planning and for sending
    return world


# --- a reminder before the due date ------------------------------------------------------------------------------------------


def test_a_person_reminded_before_gets_a_direct_message_the_day_before_it_is_due(w):
    w.reminded_before("wei.chen")

    results = w.run()

    (nudge,) = w.by_commitment(results, 3, "nudge")  # #3: wei, due tomorrow
    assert nudge.status == fu.SENT
    (message,) = [m for m in w.sent() if m["target"] == "wei.chen"]
    assert message["action_type"] == "direct_message" and "Filter persistence should land by 2026-09-19." in message["content"]
    assert "Hi Wei," in message["content"] and "due tomorrow" in message["content"]


def test_a_reminder_is_a_direct_message_not_a_channel_post(w):
    w.reminded_before("wei.chen", "mateo.silva", "noah.becker")

    w.run()

    assert w.sent() and all(m["action_type"] == "direct_message" for m in w.sent())  # never into the channel


def test_a_commitment_due_today_is_reminded_today(w):
    w.reminded_before("mateo.silva")

    results = w.run()

    (nudge,) = w.by_commitment(results, 1, "nudge")  # #1: mateo, "end of week" = today
    assert nudge.status == fu.SENT and "due today" in next(m for m in w.sent() if m["target"] == "mateo.silva")["content"]


def test_a_commitment_not_yet_due_is_left_alone(w):
    results = w.run()

    assert [r.status for r in w.by_commitment(results, 2)] == [fu.NOT_DUE]  # #2: due 25 Sep


def test_how_early_to_remind_is_configuration_not_a_literal(w):
    w.reminded_before("olivia.dupont")
    assert not [r for r in w.run().__iter__() if r.commitment_id == 2 and r.action == "nudge"]  # a week away: not at 1 day

    results = w.run(settings=replace(SETTINGS, nudge_days_before_due=7))

    assert [r.status for r in w.by_commitment(results, 2, "nudge")] == [fu.SENT]  # the same commitment, at 7 days


def test_each_commitment_is_reminded_once(w):
    w.reminded_before("wei.chen")
    w.run()

    again = w.run()

    assert [r.status for r in w.by_commitment(again, 3, "nudge")] == [fu.ALREADY_DONE]
    assert len([m for m in w.sent() if m["target"] == "wei.chen"]) == 1


def test_the_reminder_is_entered_in_the_shared_ledger_and_recorded(w):
    w.reminded_before("wei.chen")

    w.run()

    assert w.tracker.event(3, NUDGED)["day"] == "2026-09-18"
    assert SharedNudgeCap(db_path=w.db, cap=1).reading("wei.chen", "2026-09-18").sent_by_p2 == 1


def test_an_automatic_reminder_is_recorded_as_the_systems_not_a_persons(w):
    w.reminded_before("wei.chen")
    w.run()

    proposal = next(p for p in w.store.list_by_status("applied") if p.payload.get("commitment_id") == 3)
    trail = audit_trail(proposal.id, db_path=w.db)

    assert trail.approver_id == AUTO_APPROVER and trail.status == "applied"
    assert [e["action"] for e in trail.events] == ["proposal.created", "proposal.approved", "proposal.sent"]
    assert next(e for e in trail.events if e["action"] == "proposal.approved")["details"]["automatic"] is True


# --- the first reminder to a person waits for a person --------------------------------------------------------------------------


def test_the_first_reminder_ever_to_a_person_waits_for_approval_and_sends_nothing(w):
    results = w.run()

    (nudge,) = w.by_commitment(results, 3, "nudge")
    assert nudge.status == fu.AWAITING_APPROVAL and w.sent() == []
    assert w.store.get(nudge.proposal_id).status == PENDING


def test_a_waiting_reminder_is_sent_when_a_person_approves_it(w):
    nudge = w.by_commitment(w.run(), 3, "nudge")[0]

    outcome = service.approve_and_send(nudge.proposal_id, approver_id="sharon.silva", publisher=w.log, policy=APPROVER,
                                       db_path=w.db, now=FRI)

    assert outcome.outcome == "sent" and [m["target"] for m in w.sent()] == ["wei.chen"]
    assert w.tracker.event(3, NUDGED) and SharedNudgeCap(db_path=w.db, cap=1).ever_nudged("wei.chen")


def test_a_rerun_does_not_create_a_second_waiting_reminder(w):
    first = w.by_commitment(w.run(), 3, "nudge")[0]

    second = w.by_commitment(w.run(), 3, "nudge")[0]

    assert second.proposal_id == first.proposal_id and len(w.store.list_by_status("pending")) == len({
        p.id for p in w.store.list_by_status("pending")})


def test_a_reminder_a_person_rejected_is_not_sent_by_a_later_run(w):
    first = w.by_commitment(w.run(), 3, "nudge")[0]
    service.reject(first.proposal_id, approver_id="sharon.silva", reason="not needed", policy=APPROVER, db_path=w.db)

    again = w.by_commitment(w.run(), 3, "nudge")[0]

    assert again.status == fu.REJECTED_STATUS and w.sent() == []


# --- the shared cap: never chased by two agents on one day ------------------------------------------------------------------------


def test_a_person_p1_already_reminded_today_is_not_reminded_again(w):
    w.reminded_before("wei.chen")
    w.p1_ledger([("wei.chen", "2026-09-18", "2026-09-18T02:00:00+00:00")])

    results = w.run()

    (nudge,) = w.by_commitment(results, 3, "nudge")
    assert nudge.status == fu.CAP_REACHED and "1 from P1" in nudge.detail and w.sent() == []
    planned = [p for status in ("pending", "approved", "applied") for p in w.store.list_by_status(status)]
    assert not [p for p in planned if p.payload.get("commitment_id") == 3]  # wei's reminder was not even planned


def test_p1s_reminders_in_another_channel_count_against_the_same_person(w):
    w.reminded_before("wei.chen")
    conn = sqlite3.connect(w.p1)
    conn.execute("UPDATE nudges SET channel_id = '19:some-other-channel@thread.tacv2'")
    conn.execute("INSERT INTO nudges (channel_id, member_id, date, sent_at, idempotency_key) VALUES ('19:other@thread.tacv2', 'wei.chen', '2026-09-18', '2026-09-18T02:00:00+00:00', 'z')")
    conn.commit()
    conn.close()

    assert w.by_commitment(w.run(), 3, "nudge")[0].status == fu.CAP_REACHED


def test_a_reminder_p1_sent_yesterday_does_not_count_today(w):
    w.reminded_before("wei.chen")
    w.p1_ledger([("wei.chen", "2026-09-17", "2026-09-17T02:00:00+00:00")])

    assert w.by_commitment(w.run(), 3, "nudge")[0].status == fu.SENT


def test_a_waiting_unsent_p1_nudge_does_not_use_up_the_day(w):
    w.reminded_before("wei.chen")
    w.p1_ledger([("wei.chen", "2026-09-18", None)])

    assert w.by_commitment(w.run(), 3, "nudge")[0].status == fu.SENT


def test_a_person_with_two_commitments_to_remind_is_reminded_once_a_day(w):
    """noah has #4 (due today) and #8 (4 days overdue and never reminded): one message today, not two."""
    w.reminded_before("noah.becker")

    results = w.run()

    assert [r.status for r in w.by_commitment(results, 4, "nudge")] == [fu.SENT]
    assert [r.status for r in w.by_commitment(results, 8, "nudge")] == [fu.QUEUED]
    assert len([m for m in w.sent() if m["target"] == "noah.becker"]) == 1


def test_this_agents_reminder_is_visible_to_p1_through_the_shared_ledger(w):
    """The other direction: after this agent reminds someone, the ledger P1 would read shows it."""
    w.reminded_before("wei.chen")
    w.run()

    conn = sqlite3.connect(w.db)
    rows = conn.execute("SELECT member_id, date, sent_at FROM nudges WHERE date = '2026-09-18' AND sent_at IS NOT NULL").fetchall()

    assert ("wei.chen", "2026-09-18") == rows[0][:2] and rows[0][2]


def test_p1s_ledger_is_never_written_to(w):
    w.reminded_before("wei.chen")
    w.p1_ledger([("noah.becker", "2026-09-17", "2026-09-17T02:00:00+00:00")])
    before = w.p1.read_bytes()

    w.run()

    assert w.p1.read_bytes() == before


def test_if_p1s_ledger_cannot_be_read_nobody_is_reminded(w):
    w.reminded_before("wei.chen", "mateo.silva", "noah.becker")
    w.p1.write_text("not a database")

    results = w.run()

    nudges = [r for r in results if r.action == "nudge"]
    assert nudges and {r.status for r in nudges} == {fu.CAP_UNAVAILABLE} and w.sent() == []


def test_the_cap_is_checked_again_at_the_moment_of_sending(w):
    """The first reminder waits for approval; P1 reminds the same person before the approval arrives. The approval must not send."""
    nudge = w.by_commitment(w.run(), 3, "nudge")[0]
    w.p1_ledger([("wei.chen", "2026-09-18", "2026-09-18T05:00:00+00:00")])  # P1 chases wei later that day

    outcome = service.approve_and_send(nudge.proposal_id, approver_id="sharon.silva", publisher=w.log, policy=APPROVER, db_path=w.db, now=FRI)

    assert outcome.outcome == "refused" and "1 from P1" in outcome.detail and w.sent() == []
    assert w.tracker.event(3, NUDGED) is None  # and it is not recorded as sent


def test_the_cap_value_is_configuration(w):
    w.reminded_before("noah.becker")
    w.p1_ledger([("noah.becker", "2026-09-18", "2026-09-18T02:00:00+00:00")])
    assert w.by_commitment(w.run(), 4, "nudge")[0].status == fu.CAP_REACHED  # cap 1, already reminded by P1

    results = w.run(settings=replace(SETTINGS, cap_per_person_per_day=2))

    assert w.by_commitment(results, 4, "nudge")[0].status == fu.SENT  # the same situation under a cap of 2


# --- the overdue record ------------------------------------------------------------------------------------------------------------


def test_a_commitment_past_its_due_date_gets_an_overdue_record_once(w):
    results = w.run()

    assert [r.status for r in w.by_commitment(results, 6, "overdue")] == [fu.RECORDED]  # #6: due 16 Sep
    assert w.tracker.event(6, OVERDUE)["day"] == "2026-09-18" and "2 day(s) overdue" in w.tracker.event(6, OVERDUE)["detail"]
    assert not w.by_commitment(w.run(), 6, "overdue")  # not recorded a second time


def test_an_overdue_record_sends_nothing(w):
    w.run()

    assert w.sent() == []  # nobody is messaged for an overdue record; it is a record


def test_every_overdue_commitment_is_recorded(w):
    w.run()

    assert {c.id for c in w.tracker.list() if w.tracker.event(c.id, OVERDUE)} == {5, 6, 7, 8}


# --- escalation to the lead beyond the threshold -----------------------------------------------------------------------------------


def test_a_commitment_overdue_by_more_than_the_threshold_is_escalated_to_the_lead(w):
    w.reminded_before("wei.chen")
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")  # wei was reminded about #5 earlier

    results = w.run(MON)  # #5 is 6 days overdue by Monday

    escalation = w.by_commitment(results, 5, "escalation")[0]
    assert escalation.status == fu.AWAITING_APPROVAL  # the first escalation about a person waits for a person
    proposal = w.store.get(escalation.proposal_id)
    assert proposal.payload["recipient_id"] == "noah.becker" and proposal.type == "commitment_escalation"


def test_the_escalation_carries_the_evidence_and_names_people(w):
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")
    escalation = w.by_commitment(w.run(MON), 5, "escalation")[0]

    content = w.store.get(escalation.proposal_id).payload["content"]

    for fragment in ("Wei Chen", "Should have the staging auth flow failures root-caused by 2026-09-15.", "2026-09-15", "6 days overdue",
                     "more than 3 days", "reminded on 2026-09-14", "PM-023", "blocked"):
        assert fragment in content, fragment


def test_a_commitment_exactly_at_the_threshold_is_not_escalated(w):
    """#5 is exactly 3 days overdue on Friday: 'beyond' the threshold means more than."""
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")

    results = w.run(FRI)

    assert not w.by_commitment(results, 5, "escalation")


def test_the_threshold_is_configuration_not_a_literal(w):
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")
    w.tracker.record_event(8, NUDGED, "2026-09-10", "reminded")

    at_two = w.run(FRI, settings=replace(SETTINGS, escalation_threshold_days=2))
    at_five = w.run(FRI, settings=replace(SETTINGS, escalation_threshold_days=5))

    assert {r.commitment_id for r in at_two if r.action == "escalation"} == {5, 8}  # #5 is 3 days overdue, #8 is 4
    assert not {r.commitment_id for r in at_five if r.action == "escalation"}


def test_the_lead_is_told_only_after_the_person_was_reminded_on_an_earlier_day(w):
    """#5 was never reminded: the person hears first (today), the lead a day later."""
    w.reminded_before("wei.chen")

    monday = w.run(MON)
    assert w.by_commitment(monday, 5, "nudge")[0].status == fu.SENT and not w.by_commitment(monday, 5, "escalation")  # told today
    assert [m["target"] for m in w.sent() if m["target"] == "noah.becker"] == []  # the lead has not been told

    same_day_again = w.run(MON)
    assert w.by_commitment(same_day_again, 5, "escalation")[0].status == fu.WAITING  # reminded today: the lead hears tomorrow

    tuesday = w.run(TUE)
    assert w.by_commitment(tuesday, 5, "escalation")[0].status in (fu.AWAITING_APPROVAL, fu.SENT)  # a day later, the lead hears


def test_an_escalation_after_the_first_goes_out_unattended(w):
    w.reminded_before("wei.chen")
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")
    w.tracker.record_event(3, ESCALATED, "2026-09-01", "escalated about wei earlier")  # a person was approved to escalate about wei before

    escalation = w.by_commitment(w.run(MON), 5, "escalation")[0]

    assert escalation.status == fu.SENT and [m for m in w.sent() if m["target"] == "noah.becker"]


def test_an_escalation_is_not_a_reminder_to_the_person_and_not_capped_by_it(w):
    """The lead is not being chased: the escalation to him is not counted against a person's reminder cap."""
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")
    w.tracker.record_event(3, ESCALATED, "2026-09-01", "earlier")
    w.p1_ledger([("noah.becker", "2026-09-21", "2026-09-21T02:00:00+00:00")])  # the lead already had a reminder from P1 today

    escalation = w.by_commitment(w.run(MON), 5, "escalation")[0]

    assert escalation.status == fu.SENT


def test_each_commitment_is_escalated_once(w):
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")
    w.tracker.record_event(3, ESCALATED, "2026-09-01", "earlier")
    w.run(MON)

    again = w.run(MON)

    assert w.by_commitment(again, 5, "escalation")[0].status == fu.ALREADY_DONE
    assert len([m for m in w.sent() if "Overdue commitment" in m["content"] and "Wei Chen" in m["content"]]) == 1


def test_nobody_escalates_a_commitment_the_lead_owns_to_the_lead(w):
    """noah is the lead and owns #8: there is nobody else to tell."""
    w.tracker.record_event(8, NUDGED, "2026-09-10", "reminded")

    escalation = w.by_commitment(w.run(FRI), 8, "escalation")[0]

    assert escalation.status == fu.NO_LEAD


def test_a_project_with_no_lead_escalates_nothing(w):
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")

    results = w.run(MON, settings=replace(SETTINGS, lead_id=None))

    assert w.by_commitment(results, 5, "escalation")[0].status == fu.NO_LEAD


# --- who is never chased, and when -----------------------------------------------------------------------------------------------------


def test_someone_on_leave_is_never_reminded_or_escalated_about(w):
    w.reminded_before("wei.chen")
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")

    results = w.run(MON, settings=replace(SETTINGS, exceptions=frozenset({"wei.chen"})))

    assert {r.status for r in results if r.member_id == "wei.chen"} == {fu.EXCLUDED} and not [m for m in w.sent() if m["target"] == "wei.chen"]


def test_nothing_is_sent_on_a_non_working_day_but_overdue_is_still_recorded(w):
    w.reminded_before("wei.chen")

    results = w.run(SAT)

    assert w.by_commitment(results, 3, "nudge")[0].status == fu.NON_WORKING_DAY and w.sent() == []
    assert w.tracker.event(6, OVERDUE)  # a record that it slipped is not a message


def test_a_commitment_whose_item_is_done_is_closed_not_chased(w):
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE items SET status = 'done' WHERE id = 'PM-028'")
    conn.commit()
    conn.close()
    w.reminded_before("wei.chen")

    results = w.run()

    assert w.by_commitment(results, 3)[0].status == fu.FULFILLED_STATUS and w.tracker.get(3).status == FULFILLED
    assert w.sent() == [] and w.tracker.event(3, FULFILLED)["detail"] == "PM-028 is done"


def test_a_reminder_waiting_for_approval_is_not_sent_if_the_commitment_has_since_been_closed(w):
    nudge = w.by_commitment(w.run(), 3, "nudge")[0]
    w.tracker.close(3, status=FULFILLED, day="2026-09-18", detail="done in person")

    outcome = service.approve_and_send(nudge.proposal_id, approver_id="sharon.silva", publisher=w.log, policy=APPROVER, db_path=w.db, now=FRI)

    assert outcome.outcome == "refused" and "closed since" in outcome.detail and w.sent() == []


def test_a_commitment_with_no_day_in_it_is_never_chased(w):
    w.tracker.add(member_id="aisha.rahman", text="I'll get to the retry queue when I can.", made_at="2026-09-01", source_message_id="m-1")
    w.reminded_before("aisha.rahman")

    results = w.run(MON)

    new = max(c.id for c in w.tracker.list())
    assert [r.status for r in w.by_commitment(results, new)] == [fu.NO_DATE] and not [m for m in w.sent() if m["target"] == "aisha.rahman"]


def test_a_commitment_that_came_from_an_outcome_record_is_followed_up_the_same_way(w):
    c, _ = w.tracker.add(member_id="aisha.rahman", text="I'll have the wizard copy done by 2026-09-19.", made_at="2026-09-17",
                         due_date_iso="2026-09-19", source_message_id="m-9", source="outcome")
    w.reminded_before("aisha.rahman")

    results = w.run()

    assert w.by_commitment(results, c.id, "nudge")[0].status == fu.SENT and "wizard copy" in w.sent()[0]["content"]


# --- a dry run ----------------------------------------------------------------------------------------------------------------------------


def test_a_dry_run_says_what_would_happen_and_does_nothing(w):
    w.reminded_before("wei.chen")
    w.tracker.record_event(5, NUDGED, "2026-09-14", "reminded")
    before = {t: sqlite3.connect(w.db).execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("proposals", "commitment_events", "nudges", "audit", "write_log")}

    results = w.run(FRI, dry_run=True)

    after = {t: sqlite3.connect(w.db).execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before and w.sent() == []
    assert w.by_commitment(results, 3, "nudge")[0].status == fu.WOULD_SEND  # wei was reminded before: it would go out
    assert w.by_commitment(results, 1, "nudge")[0].status == fu.WOULD_AWAIT  # mateo never was: it would wait for a person


def test_a_dry_run_applies_the_cap_to_itself(w):
    w.reminded_before("noah.becker")

    results = w.run(dry_run=True)

    assert w.by_commitment(results, 4, "nudge")[0].status == fu.WOULD_SEND and w.by_commitment(results, 8, "nudge")[0].status == fu.QUEUED


# --- settings: from P1's channel configuration, overridable -----------------------------------------------------------------------------------


def test_the_settings_come_from_p1s_channel_config():
    settings = load_followup_settings(env={})

    assert (settings.lead_id, settings.escalation_threshold_days, settings.cap_per_person_per_day) == ("noah.becker", 3, 1)
    assert settings.timezone == "Asia/Colombo" and settings.nudge_days_before_due == 1 and settings.exceptions == frozenset()


def test_the_environment_overrides_the_settings():
    settings = load_followup_settings(env={"PM_COMMITMENT_NUDGE_DAYS_BEFORE": "2", "PM_COMMITMENT_ESCALATION_DAYS": "5",
                                           "PM_NUDGE_CAP_PER_PERSON_PER_DAY": "2", "PM_LEAD_ID": "sharon.silva"})

    assert (settings.nudge_days_before_due, settings.escalation_threshold_days, settings.cap_per_person_per_day, settings.lead_id) == (2, 5, 2, "sharon.silva")


@pytest.mark.parametrize("name", ["PM_COMMITMENT_NUDGE_DAYS_BEFORE", "PM_COMMITMENT_ESCALATION_DAYS", "PM_NUDGE_CAP_PER_PERSON_PER_DAY"])
def test_a_bad_setting_is_an_error_not_a_default(name):
    with pytest.raises(ValueError):
        load_followup_settings(env={name: "soon"})


def test_the_json_log_entries_are_what_a_reader_would_expect(w):
    w.reminded_before("wei.chen")
    w.run()

    entry = w.sent()[0]

    assert set(entry) >= {"action_type", "target", "content", "logged_at"} and json.dumps(entry)  # a real, serialisable DM record


def test_nothing_in_this_module_names_a_threshold_number():
    import re

    source = Path(fu.__file__).read_text()
    code = re.sub(r'""".*?"""|#.*', "", source, flags=re.DOTALL)

    assert not re.search(r"(>|>=|<|<=)\s*[1-9]\d*\b", code)  # no comparison against a literal day count or cap


def test_unused_names_are_kept_honest():
    assert date(2026, 9, 18).isoformat() == "2026-09-18" and REJECTED == "rejected"
