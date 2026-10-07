"""PM-25, golden case 7: nudges never exceed the shared per-person daily cap (counting P1's), and a reminder always
comes before an escalation, without anything repeating beyond the cap.

The case runs P1's real nudge job and P2's real follow-up against each other for three working days on the seed's
commitments, each agent three times a day, with the delivery recorded at the publisher (so it does not depend on
either agent's own ledger). These tests check three things:

  the analysers   each rule is a plain function of the deliveries, tested here on hand-made logs, one violation at a time
  the scenario    the hand-labelled schedule of who was reminded by which agent on which day is exactly what happened
  the numbers     each guard is broken in turn (a cap blind to P1, a cap blind to P2, an escalation that ignores the
                  rule that the person hears first, a send that is never recorded) and the matching number notices
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval

from pm.commitments import followup as fu
from pm.commitments.cap import SharedNudgeCap
from pm.eval import pm25_cases as gc7
from pm.eval.pm25_cases import Delivery
from pm.eval.registrations import register_all

REPO = Path(__file__).resolve().parents[2]

FRI, MON, TUE = "2026-09-18", "2026-09-21", "2026-09-22"


def _d(agent, day, person, kind="nudge", *, commitment=None, recipient=None, content="x") -> Delivery:
    return Delivery(agent=agent, day=day, recipient=recipient or person, person=person, kind=kind, commitment_id=commitment, content=content)


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> gc7.Run:
    return gc7.run_scenario(tmp_path_factory.mktemp("gc7"))


@pytest.fixture(scope="module")
def control(tmp_path_factory) -> gc7.Run:
    return gc7.run_scenario(tmp_path_factory.mktemp("gc7_control"), shared_cap=False)


# --- the analysers, one violation at a time ------------------------------------------------------------------------------


def test_cap_breaches_counts_both_agents_nudges_for_one_person_one_day():
    clean = [_d("p1", FRI, "wei"), _d("p2", MON, "wei")]  # different days: fine
    over = [_d("p1", FRI, "wei"), _d("p2", FRI, "wei")]  # the same person chased twice the same day, by two agents

    assert gc7.cap_breaches(clean, cap=1) == []
    assert gc7.cap_breaches(over, cap=1) == [("wei", FRI, 2)]
    assert gc7.cap_breaches(over, cap=2) == []  # the cap is a number, not a literal


def test_an_escalation_is_not_a_nudge_for_the_cap():
    deliveries = [_d("p2", FRI, "wei"), _d("p2", FRI, "wei", "escalation", recipient="noah")]

    assert gc7.cap_breaches(deliveries, cap=1) == []


def test_the_same_person_under_two_ids_is_still_one_person():
    deliveries = [_d("p1", FRI, "wei", recipient="aad-wei-0001"), _d("p2", FRI, "wei", recipient="wei.chen")]

    assert gc7.cap_breaches(deliveries, cap=1) == [("wei", FRI, 2)]


def test_an_escalation_needs_an_earlier_days_reminder_about_the_same_commitment():
    ok = [_d("p2", MON, "wei", commitment=5), _d("p2", TUE, "wei", "escalation", commitment=5, recipient="noah")]
    same_day = [_d("p2", TUE, "wei", commitment=5), _d("p2", TUE, "wei", "escalation", commitment=5, recipient="noah")]
    never = [_d("p2", TUE, "wei", "escalation", commitment=5, recipient="noah")]
    other_commitment = [_d("p2", MON, "wei", commitment=3), _d("p2", TUE, "wei", "escalation", commitment=5, recipient="noah")]
    after = [_d("p2", TUE, "wei", "escalation", commitment=5, recipient="noah"), _d("p2", "2026-09-23", "wei", commitment=5)]

    assert gc7.order_violations(ok) == []
    assert len(gc7.order_violations(same_day)) == 1  # the lead hears a day after the person, never the same day
    assert len(gc7.order_violations(never)) == 1
    assert len(gc7.order_violations(other_commitment)) == 1  # a reminder about something else is not the reminder
    assert len(gc7.order_violations(after)) == 1


def test_repeats_finds_a_second_reminder_or_escalation_for_one_commitment_and_an_exact_resend():
    once = [_d("p2", MON, "wei", commitment=5), _d("p2", TUE, "wei", "escalation", commitment=5, recipient="noah")]
    twice_nudged = once + [_d("p2", TUE, "wei", commitment=5, content="again")]
    twice_escalated = once + [_d("p2", "2026-09-23", "wei", "escalation", commitment=5, recipient="noah", content="again")]
    resent = once + [once[0]]

    assert gc7.repeats(once) == []
    assert len(gc7.repeats(twice_nudged)) == 1
    assert len(gc7.repeats(twice_escalated)) == 1
    assert len(gc7.repeats(resent)) >= 1


def test_schedule_mismatches_is_the_symmetric_difference_with_the_hand_labelled_schedule():
    expected = frozenset({("p1", FRI, "wei", "nudge"), ("p2", FRI, "noah", "nudge")})

    assert gc7.schedule_mismatches([_d("p1", FRI, "wei"), _d("p2", FRI, "noah")], expected) == []
    extra = gc7.schedule_mismatches([_d("p1", FRI, "wei"), _d("p2", FRI, "noah"), _d("p2", FRI, "wei")], expected)
    missing = gc7.schedule_mismatches([_d("p1", FRI, "wei")], expected)
    assert extra == [("p2", FRI, "wei", "nudge")] and missing == [("p2", FRI, "noah", "nudge")]


# --- the scenario against the hand-labelled schedule ------------------------------------------------------------------------


def test_who_was_reminded_by_which_agent_on_which_day_is_exactly_the_hand_labelled_schedule(run):
    assert gc7.schedule_mismatches(run.deliveries) == []
    kinds = sorted((d.agent, d.day, d.person, d.kind) for d in run.deliveries)
    assert len(kinds) == len(gc7.EXPECTED) == 13  # 10 reminders and 3 escalations, nothing else


def test_a_reminder_approved_late_is_refused_at_sending_when_the_other_agent_got_there_first(run):
    # P2's first ever reminder to Mateo waited for a person on Tuesday; P1 reminded him that day; the approval came after.
    assert run.refused_at_send == {("p2", TUE, "mateo.silva")}
    assert [(d.agent, d.kind) for d in run.deliveries if d.day == TUE and d.person == "mateo.silva"] == [("p1", "nudge")]


def test_the_cap_actually_held_each_agent_back_in_both_directions(run):
    # P1 reminded Wei and Mateo first on Friday, so P2 held back; P2 reminded Wei and Dupont first on Monday, so P1 held back.
    assert run.held == {("p2", FRI, "wei.chen"), ("p2", FRI, "mateo.silva"), ("p1", MON, "wei.chen"), ("p1", MON, "olivia.dupont")}


def test_a_reminder_still_waiting_for_a_persons_approval_does_not_count_against_the_cap(run):
    # P1 chased Noah on Friday, but its first ever reminder to him was never approved: he was reminded once, by P2, and the
    # P2 reminder itself only went out once a person approved it.
    friday_noah = [d for d in run.deliveries if d.day == FRI and d.person == "noah.becker"]

    assert [(d.agent, d.kind) for d in friday_noah] == [("p2", "nudge")]
    assert gc7.cap_breaches(run.deliveries) == []


def test_each_escalation_is_to_the_lead_and_a_day_after_the_persons_reminder(run):
    escalations = [d for d in run.deliveries if d.kind == "escalation"]

    assert {d.person for d in escalations} == {"wei.chen", "olivia.dupree", "olivia.dupont"}
    assert {d.recipient for d in escalations} == {"noah.becker"} and {d.day for d in escalations} == {TUE}
    assert gc7.order_violations(run.deliveries) == []


def test_nobody_is_reminded_twice_in_a_day_by_the_two_agents_together(run):
    assert gc7.cap_breaches(run.deliveries) == []
    assert gc7.ledger_mismatches(run) == []  # and the two ledgers the cap reads say what was actually delivered


def test_running_each_agent_three_times_a_day_repeats_nothing(run):
    assert gc7.repeats(run.deliveries) == []
    assert run.runs_per_agent_per_day == 3


def test_the_metrics_all_pass_and_none_of_them_is_vacuous(run, control):
    results = {m.metric_id: m for m in gc7.metrics(run, control)}

    assert all(m.passed for m in results.values()), [m.metric_id for m in results.values() if not m.passed]
    assert results["GC7-cap-breach-count"].measured == 0
    assert results["GC7-escalation-order-violation-count"].measured == 0
    assert results["GC7-repeat-count"].measured == 0
    assert results["GC7-schedule-mismatch-count"].measured == 0
    assert results["GC7-ledger-delivery-mismatch-count"].measured == 0
    assert results["GC7-held-back-by-the-other-agent-count"].measured == 4 and results["GC7-escalations-delivered-count"].measured == 3
    assert results["GC7-refused-at-send-count"].measured == 1
    assert results["GC7-control-breach-count"].measured >= 1  # with the shared cap off, the same estate does chase people twice


def test_without_the_shared_cap_the_same_estate_chases_people_twice(control):
    breaches = gc7.cap_breaches(control.deliveries)

    assert breaches, "the control must show the double-chase the shared cap prevents"
    assert {person for person, _, _ in breaches} <= {"wei.chen", "mateo.silva", "olivia.dupont", "noah.becker"}


# --- break each guard, and the matching number notices ----------------------------------------------------------------------


def test_a_cap_in_p2_that_cannot_see_p1_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(SharedNudgeCap, "_sent_by_p1", lambda self, member_id, day: (0, "read"))

    broken = gc7.run_scenario(tmp_path)

    assert gc7.cap_breaches(broken.deliveries)  # P2 reminded Wei and Mateo on top of P1's reminders
    assert gc7.schedule_mismatches(broken.deliveries)


def test_a_cap_in_p1_that_cannot_see_p2_is_caught(tmp_path, monkeypatch):
    import p1.nudges.nudge_job as p1_job

    monkeypatch.setattr(p1_job, "shared_cap_from_environment", lambda db_path, env=None: None)

    broken = gc7.run_scenario(tmp_path)

    assert gc7.cap_breaches(broken.deliveries)  # P1 reminded Wei and Dupont on top of P2's
    assert gc7.schedule_mismatches(broken.deliveries)


def test_a_send_that_is_never_recorded_in_the_ledger_is_caught(tmp_path, monkeypatch):
    from pm.commitments import delivery

    monkeypatch.setattr(delivery, "after_send", lambda proposal, db_path, now: None)  # delivered, but the ledger never hears

    broken = gc7.run_scenario(tmp_path)

    assert gc7.ledger_mismatches(broken)  # the ledger the other agent reads says nobody was reminded
    assert gc7.cap_breaches(broken.deliveries)


def test_an_escalation_that_ignores_the_rule_that_the_person_hears_first_is_caught(tmp_path, monkeypatch):
    original = fu._Run._escalate

    def impatient(self, c, due_text, overdue_days, events):
        """Tell the lead the moment the commitment is past the threshold, whether or not the person was reminded first."""
        from pm.approval.proposals import ESCALATION_PROPOSAL_TYPE
        from pm.commitments import messages

        lead = self.settings.lead_id
        if not lead or lead == c.member_id or fu.ESCALATED in events:
            return original(self, c, due_text, overdue_days, events)
        content = messages.escalation_text(
            c, owner_name=self._name(c.member_id), due=due_text, overdue_days=overdue_days,
            threshold_days=self.settings.escalation_threshold_days, nudge_day=self.today_iso, item_status=None,
        )
        return self._send(c, ESCALATION_PROPOSAL_TYPE, f"commitment_escalation:{c.id}", lead, content, False, "escalation", reason="impatient")

    monkeypatch.setattr(fu._Run, "_escalate", impatient)

    broken = gc7.run_scenario(tmp_path)

    assert gc7.order_violations(broken.deliveries)
    assert gc7.schedule_mismatches(broken.deliveries)


def test_the_scenario_leaves_the_environment_as_it_found_it(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("P1_DB_PATH", "/somewhere/else.db")
    monkeypatch.delenv("P1_SHARED_NUDGE_CAP_PER_DAY", raising=False)
    before = {k: os.environ.get(k) for k in ("P1_DB_PATH", "P1_SHARED_NUDGE_CAP_PER_DAY", "P1_PEER_NUDGE_LEDGERS")}

    gc7.run_scenario(tmp_path)

    assert {k: os.environ.get(k) for k in before} == before


# --- registered, printed, recorded; and the labels follow from the rules ------------------------------------------------------


def test_it_is_registered_and_the_numbers_are_recorded(tmp_path):
    registry = GoldenCaseRegistry()
    register_all(registry)
    assert "GC7" in {case.case_id for case in registry.all_cases()}
    results_path = tmp_path / "results.jsonl"

    summary = run_eval(registry, model_id="scripted", results_path=results_path)

    ids = {r.metric_id for r in summary.results}
    assert {"GC7-cap-breach-count", "GC7-escalation-order-violation-count", "GC7-repeat-count", "GC7-schedule-mismatch-count",
            "GC7-ledger-delivery-mismatch-count", "GC7-held-back-by-the-other-agent-count", "GC7-refused-at-send-count",
            "GC7-escalations-delivered-count", "GC7-control-breach-count"} <= ids
    assert summary.all_passed
    recorded = {r["metric_id"] for line in results_path.read_text().splitlines() for r in json.loads(line)["results"]}
    assert "GC7-cap-breach-count" in recorded


def test_the_eval_script_prints_the_case(monkeypatch, tmp_path, capfd):
    spec = importlib.util.spec_from_file_location("run_eval_script_25", REPO / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RESULTS_PATH", tmp_path / "results.jsonl")
    monkeypatch.setattr("sys.argv", ["run_eval.py"])

    code = module.main()

    out = capfd.readouterr().out
    assert code == 0 and "Golden case 7" in out and "GC7-cap-breach-count" in out
    assert "control, shared cap off on both sides" in out and "held back by the other agent" in out


def test_the_hand_labelled_schedule_follows_from_the_rules_by_plain_arithmetic():
    """EXPECTED is a literal, but it must agree with the rules it was worked out from."""
    reminders = [e for e in gc7.EXPECTED if e[3] == "nudge"]
    escalations = [e for e in gc7.EXPECTED if e[3] == "escalation"]

    # one agent per person per day: no (person, day) appears twice among the reminders
    assert len({(person, day) for _, day, person, _ in reminders}) == len(reminders) == 10
    # every escalation is the day after the same person's reminder from P2, and only for people who are not the lead
    for _, day, person, _ in escalations:
        assert person != "noah.becker"
        assert any(a == "p2" and p == person and d == "2026-09-21" for a, d, p, _ in reminders) and day == "2026-09-22"
    # P2's reminders are never on a person-day where P1 also reminded them
    p1_days = {(person, day) for agent, day, person, _ in reminders if agent == "p1"}
    assert not {(person, day) for agent, day, person, _ in reminders if agent == "p2"} & p1_days


def test_a_reminder_that_is_not_checked_again_at_the_moment_of_sending_is_caught(tmp_path, monkeypatch):
    from pm.commitments import delivery

    monkeypatch.setattr(delivery, "pre_send_problem", lambda proposal, db_path, now: None)  # the second look, at sending, is gone

    broken = gc7.run_scenario(tmp_path)

    assert ("mateo.silva" in {person for person, _, _ in gc7.cap_breaches(broken.deliveries)})  # approved late, sent on top of P1's
    assert not broken.refused_at_send


def test_either_layer_alone_stops_a_second_reminder_in_one_run_but_not_neither(tmp_path, monkeypatch):
    """The in-run queue ("one reminder a person a day, even while the first is waiting") and the check at the moment of
    sending both stop Noah being reminded twice on Friday. Taking away one leaves the other; taking away both does not."""
    from pm.commitments import delivery

    class _NeverQueued(set):
        def __contains__(self, item):
            return False

    original_init = fu._Run.__init__

    def init_without_the_queue(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.queued_today = _NeverQueued()

    monkeypatch.setattr(fu._Run, "__init__", init_without_the_queue)
    assert gc7.cap_breaches(gc7.run_scenario(tmp_path / "queue_only").deliveries) == []  # the send-time check still holds

    monkeypatch.setattr(delivery, "pre_send_problem", lambda proposal, db_path, now: None)
    assert ("noah.becker" in {person for person, _, _ in gc7.cap_breaches(gc7.run_scenario(tmp_path / "neither").deliveries)})
