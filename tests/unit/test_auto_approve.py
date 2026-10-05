"""PM-13, unattended mode: auto-approve.

Switched on (PM_AUTO_APPROVE=1), the system approves a morning brief itself so
nobody has to click -- but only when it is safe to, and always on the record:

- every line grounded: nothing dropped by grounding, no "[as recorded]" fallback
  (anything the model got wrong still waits for a person);
- unless configured otherwise, a person has approved a brief for that channel
  before (the first post to a channel is never unattended);
- the same scope rules as a human approval (a real publisher only posts to an
  allowlisted channel);
- the actor is "system:auto-approve", never a person, and that id can never be
  used by a human.

Off by default. A failed send leaves it approved and retryable.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest
from p1.adapters.teams_publisher import TeamsPublisher
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import APPLIED, APPROVED, PENDING, ProposalStore

from pm.approval.audit import audit_trail, describe
from pm.approval.service import (
    AUTO_APPROVER,
    ApprovalPolicy,
    approve_and_send,
    auto_approve_and_send,
    load_approval_policy,
    send_approved,
)
from pm.eval.pm12_cases import FORGERIES, ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.reporting.facts import compute_morning_brief_facts
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import build_current_snapshot

MON = datetime(2026, 9, 14, 2, 30, tzinfo=timezone.utc)
TUE = datetime(2026, 9, 15, 2, 30, tzinfo=timezone.utc)
HUMAN = "sharon.silva"
TEST_CHANNEL = "19:test-channel@thread.tacv2"

ON = ApprovalPolicy(approver_ids=frozenset({HUMAN}), auto_approve=True)
ON_NO_FIRST_HUMAN = ApprovalPolicy(
    approver_ids=frozenset({HUMAN}), auto_approve=True, auto_approve_requires_first_human=False
)
OFF = ApprovalPolicy(approver_ids=frozenset({HUMAN}))


def _config(**overrides):
    values = {
        "channel_id": CHANNEL_ID, "timezone": "Asia/Colombo", "working_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "morning_brief_time": time(8, 0), "end_of_day_time": time(17, 0),
    }
    return ProjectScheduleConfig(**{**values, **overrides})


def _job(db, moment, policy, publisher, gateway=None, **config_overrides):
    return run_morning_brief_job(
        _config(**config_overrides), gateway or ScriptedGateway(), moment=moment, db_path=db,
        publisher=publisher, policy=policy,
    )


def _forging_gateway(db, name, moment=MON):
    snapshot = build_current_snapshot(db, taken_at=moment.isoformat(), tz_name="Asia/Colombo")
    return ScriptedGateway(persistent_tamper=FORGERIES[name].tamper(compute_morning_brief_facts(snapshot)))


class _Recording(TeamsPublisher):
    """A real-kind publisher (not the log-only one) that records its posts."""

    def __init__(self, fail=False):
        self.fail = fail
        self.posts: list[tuple[str, str]] = []

    def post_channel_message(self, channel_id, content):
        if self.fail:
            raise ConnectionError("down")
        self.posts.append((channel_id, content))
        return {"ok": True}

    def post_direct_message(self, member_id, content):
        raise AssertionError("never used")


def _real_policy(**changes):
    return ApprovalPolicy(
        approver_ids=frozenset({HUMAN}), allowlisted_channel_ids=[TEST_CHANNEL], auto_approve=True,
        auto_approve_requires_first_human=False, **changes,
    )


@pytest.fixture()
def log(tmp_path):
    return LogPublisher(tmp_path / "log.jsonl")


# --- off by default -------------------------------------------------------------------


def test_it_is_off_unless_switched_on(seeded_db_path, log):
    result = _job(seeded_db_path, TUE, OFF, log)

    assert result.delivery_status == "proposed" and log.read_log() == []
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING


# --- the first post to a channel is never unattended -------------------------------------


def test_the_first_brief_for_a_channel_waits_for_a_person(seeded_db_path, log):
    result = _job(seeded_db_path, MON, ON, log)

    assert result.delivery_status == "proposed" and "first" in result.delivery_detail.lower()
    assert log.read_log() == [] and ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING


def test_after_a_person_has_approved_one_later_briefs_go_out_unattended(seeded_db_path, log):
    first = _job(seeded_db_path, MON, ON, log)
    approve_and_send(first.proposal_id, approver_id=HUMAN, publisher=log, policy=ON, db_path=seeded_db_path)

    second = _job(seeded_db_path, TUE, ON, log)

    assert second.delivery_status == "auto_sent"
    assert len(log.read_log()) == 2
    proposal = ProposalStore(seeded_db_path).get(second.proposal_id)
    assert proposal.status == APPLIED and proposal.approver_id == AUTO_APPROVER
    assert proposal.payload["content"] == proposal.original_model_output["content"]  # never edited


def test_an_earlier_brief_nobody_decided_does_not_count_as_a_first_approval(seeded_db_path, log):
    _job(seeded_db_path, MON, ON, log)  # left pending

    second = _job(seeded_db_path, TUE, ON, log)

    assert second.delivery_status == "proposed" and log.read_log() == []


def test_an_earlier_approved_but_never_sent_brief_does_not_count_either(seeded_db_path):
    policy = ApprovalPolicy(approver_ids=frozenset({HUMAN}), allowlisted_channel_ids=[TEST_CHANNEL], auto_approve=True)
    down = _Recording(fail=True)
    first = _job(seeded_db_path, MON, policy, down, publish_channel_id=TEST_CHANNEL)
    approve_and_send(first.proposal_id, approver_id=HUMAN, publisher=down, policy=policy, db_path=seeded_db_path)
    assert ProposalStore(seeded_db_path).get(first.proposal_id).status == APPROVED

    second = _job(seeded_db_path, TUE, policy, down, publish_channel_id=TEST_CHANNEL)

    assert second.delivery_status == "proposed"


def test_each_target_channel_needs_its_own_first_approval(seeded_db_path, log):
    first = _job(seeded_db_path, MON, ON, log)
    approve_and_send(first.proposal_id, approver_id=HUMAN, publisher=log, policy=ON, db_path=seeded_db_path)

    other = _job(seeded_db_path, TUE, ON, log, publish_channel_id="19:another@thread.tacv2")

    assert other.delivery_status == "proposed"


def test_an_earlier_automatic_approval_does_not_count_as_a_persons(seeded_db_path, log):
    _job(seeded_db_path, MON, ON_NO_FIRST_HUMAN, log)  # sent automatically, with the rule switched off

    later = _job(seeded_db_path, TUE, ON, log)  # rule back on: still no person has approved one

    assert later.delivery_status == "proposed" and len(log.read_log()) == 1


def test_the_first_approval_rule_can_be_switched_off(seeded_db_path, log):
    result = _job(seeded_db_path, MON, ON_NO_FIRST_HUMAN, log)

    assert result.delivery_status == "auto_sent" and len(log.read_log()) == 1


# --- only a fully grounded brief goes unattended -------------------------------------------


@pytest.mark.parametrize("name", ["invented_id", "embellishment", "nonexistent_reference"])
def test_a_brief_where_grounding_dropped_a_line_is_held_for_a_person(seeded_db_path, log, name):
    result = _job(seeded_db_path, MON, ON_NO_FIRST_HUMAN, log, gateway=_forging_gateway(seeded_db_path, name))

    assert result.delivery_status == "proposed" and "dropped" in result.delivery_detail.lower()
    assert log.read_log() == []
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING


def test_a_held_brief_can_still_be_approved_by_a_person(seeded_db_path, log):
    held = _job(seeded_db_path, MON, ON_NO_FIRST_HUMAN, log, gateway=_forging_gateway(seeded_db_path, "invented_id"))

    outcome = approve_and_send(held.proposal_id, approver_id=HUMAN, publisher=log, policy=ON, db_path=seeded_db_path)

    assert outcome.outcome == "sent"
    assert ProposalStore(seeded_db_path).get(held.proposal_id).approver_id == HUMAN


# --- scope and identity --------------------------------------------------------------------


def test_a_human_can_never_approve_as_the_system(seeded_db_path, log):
    result = _job(seeded_db_path, MON, ON, log)
    listed = ApprovalPolicy(approver_ids=frozenset({AUTO_APPROVER}))

    outcome = approve_and_send(result.proposal_id, approver_id=AUTO_APPROVER, publisher=log, policy=listed, db_path=seeded_db_path)

    assert outcome.outcome == "refused" and log.read_log() == []


def test_a_real_publisher_off_the_allowlist_is_never_auto_sent(seeded_db_path):
    real = _Recording()

    result = _job(seeded_db_path, MON, ON_NO_FIRST_HUMAN, real)  # targets the non-allowlisted seeded channel

    assert result.delivery_status == "proposed" and "allowlist" in result.delivery_detail and real.posts == []
    # and it was not approved on the way: a person can still decide it
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING


def test_the_service_enforces_its_own_switch_whoever_calls_it(seeded_db_path, log):
    """auto_approve_and_send must refuse on its own when auto-approve is off,
    not rely on the job having checked."""
    pending = _job(seeded_db_path, MON, OFF, log)

    outcome = auto_approve_and_send(pending.proposal_id, publisher=log, policy=OFF, db_path=seeded_db_path)

    assert outcome.outcome == "held" and "off" in outcome.detail
    assert ProposalStore(seeded_db_path).get(pending.proposal_id).status == PENDING and log.read_log() == []


def test_the_service_will_not_auto_approve_what_is_no_longer_pending(seeded_db_path, log):
    sent = _job(seeded_db_path, MON, ON_NO_FIRST_HUMAN, log)

    outcome = auto_approve_and_send(sent.proposal_id, publisher=log, policy=ON_NO_FIRST_HUMAN, db_path=seeded_db_path)

    assert outcome.outcome == "refused" and len(log.read_log()) == 1


def test_a_real_publisher_on_the_allowlist_is_auto_sent_once(seeded_db_path):
    real = _Recording()
    policy = _real_policy()

    first = _job(seeded_db_path, MON, policy, real, publish_channel_id=TEST_CHANNEL)
    again = _job(seeded_db_path, MON, policy, real, publish_channel_id=TEST_CHANNEL)

    assert first.delivery_status == "auto_sent" and again.delivery_status == "already_proposed"
    assert len(real.posts) == 1 and real.posts[0][0] == TEST_CHANNEL


# --- on the record ----------------------------------------------------------------------------


def test_the_trail_shows_an_automatic_approval_and_says_no_person_reviewed_it(seeded_db_path, log):
    first = _job(seeded_db_path, MON, ON, log)
    approve_and_send(first.proposal_id, approver_id=HUMAN, publisher=log, policy=ON, db_path=seeded_db_path)
    second = _job(seeded_db_path, TUE, ON, log)

    trail = audit_trail(second.proposal_id, db_path=seeded_db_path)

    assert trail.approver_id == AUTO_APPROVER and trail.decided_at and trail.edited is False
    assert [(e["actor"], e["action"]) for e in trail.events] == [
        ("agent", "proposal.created"), (AUTO_APPROVER, "proposal.approved"), (AUTO_APPROVER, "proposal.sent"),
    ]
    approved = trail.events[1]["details"]
    assert approved["automatic"] is True and approved["reason"]
    text = describe(trail).lower()
    assert "automatically" in text and "no person" in text and "morning brief" in text


def test_a_failed_auto_send_leaves_it_approved_and_a_retry_sends_it_once(seeded_db_path):
    flaky = _Recording(fail=True)
    policy = _real_policy()

    result = _job(seeded_db_path, MON, policy, flaky, publish_channel_id=TEST_CHANNEL)

    assert result.delivery_status == "auto_send_failed"
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == APPROVED
    flaky.fail = False
    assert send_approved(result.proposal_id, publisher=flaky, policy=policy, db_path=seeded_db_path).outcome == "sent"
    assert send_approved(result.proposal_id, publisher=flaky, policy=policy, db_path=seeded_db_path).outcome == "refused"
    assert len(flaky.posts) == 1


def test_if_the_policy_cannot_be_loaded_the_brief_is_proposed_not_sent(seeded_db_path, log, monkeypatch):
    from pm.jobs import morning_brief_job

    def broken():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(morning_brief_job, "load_approval_policy", broken)

    result = run_morning_brief_job(_config(), ScriptedGateway(), moment=MON, db_path=seeded_db_path, publisher=log)

    assert result.delivery_status == "proposed" and log.read_log() == []


# --- configuration ------------------------------------------------------------------------------


def test_auto_approve_is_on_only_when_the_switch_is_exactly_one(monkeypatch):
    monkeypatch.delenv("PM_AUTO_APPROVE", raising=False)
    assert load_approval_policy().auto_approve is False
    for value in ("0", "yes", "true", "", "on"):
        monkeypatch.setenv("PM_AUTO_APPROVE", value)
        assert load_approval_policy().auto_approve is False, value
    monkeypatch.setenv("PM_AUTO_APPROVE", "1")
    assert load_approval_policy().auto_approve is True


def test_the_first_approval_rule_is_on_unless_explicitly_zero(monkeypatch):
    monkeypatch.delenv("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", raising=False)
    assert load_approval_policy().auto_approve_requires_first_human is True
    monkeypatch.setenv("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", "0")
    assert load_approval_policy().auto_approve_requires_first_human is False
    monkeypatch.setenv("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", "anything else")
    assert load_approval_policy().auto_approve_requires_first_human is True
