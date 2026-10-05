"""Why is a brief waiting for a person? The service answers, so a page or a
script can say so; it must agree exactly with what auto_approve_and_send does.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import ProposalStore

from pm.approval.service import (
    ApprovalPolicy,
    approve_and_send,
    auto_approve_and_send,
    explain_hold,
)
from pm.eval.pm12_cases import FORGERIES, ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.reporting.facts import compute_morning_brief_facts
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import build_current_snapshot

MON = datetime(2026, 9, 14, 2, 30, tzinfo=timezone.utc)
TUE = datetime(2026, 9, 15, 2, 30, tzinfo=timezone.utc)
OFF = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
ON = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), auto_approve=True)
ON_NO_FIRST = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), auto_approve=True, auto_approve_requires_first_human=False)


def _config():
    return ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )


@pytest.fixture()
def log(tmp_path):
    return LogPublisher(tmp_path / "log.jsonl")


def _pending(db, moment=MON, gateway=None):
    return run_morning_brief_job(_config(), gateway or ScriptedGateway(), moment=moment, db_path=db, policy=OFF).proposal_id


def test_it_says_when_auto_approve_is_off(seeded_db_path, log):
    pid = _pending(seeded_db_path)

    assert "auto-approve is off" in explain_hold(pid, policy=OFF, publisher=log, db_path=seeded_db_path)


def test_it_says_when_it_is_waiting_for_a_first_approval(seeded_db_path, log):
    pid = _pending(seeded_db_path)

    assert "first brief for this channel" in explain_hold(pid, policy=ON, publisher=log, db_path=seeded_db_path)


def test_it_says_when_grounding_dropped_lines(seeded_db_path, log):
    snapshot = build_current_snapshot(seeded_db_path, taken_at=MON.isoformat(), tz_name="Asia/Colombo")
    gateway = ScriptedGateway(persistent_tamper=FORGERIES["invented_id"].tamper(compute_morning_brief_facts(snapshot)))
    pid = _pending(seeded_db_path, gateway=gateway)

    assert "dropped" in explain_hold(pid, policy=ON_NO_FIRST, publisher=log, db_path=seeded_db_path)


def test_it_says_when_the_target_is_off_the_allowlist_for_a_real_publisher(seeded_db_path):
    from p1.adapters.teams_publisher import TeamsPublisher

    class Real(TeamsPublisher):
        def post_channel_message(self, channel_id, content):
            raise AssertionError

        def post_direct_message(self, member_id, content):
            raise AssertionError

    pid = _pending(seeded_db_path)

    assert "allowlist" in explain_hold(pid, policy=ON_NO_FIRST, publisher=Real(), db_path=seeded_db_path)


def test_it_returns_none_when_nothing_is_holding_it_back(seeded_db_path, log):
    pid = _pending(seeded_db_path)

    assert explain_hold(pid, policy=ON_NO_FIRST, publisher=log, db_path=seeded_db_path) is None


def test_it_stops_waiting_for_a_first_approval_once_a_person_has_given_one(seeded_db_path, log):
    first = _pending(seeded_db_path, MON)
    second = _pending(seeded_db_path, TUE)
    assert "first brief" in explain_hold(second, policy=ON, publisher=log, db_path=seeded_db_path)

    approve_and_send(first, approver_id="sharon.silva", publisher=log, policy=ON, db_path=seeded_db_path)

    assert explain_hold(second, policy=ON, publisher=log, db_path=seeded_db_path) is None


def test_it_agrees_with_what_auto_approve_actually_does(seeded_db_path, log):
    pid = _pending(seeded_db_path)
    reason = explain_hold(pid, policy=ON, publisher=log, db_path=seeded_db_path)

    outcome = auto_approve_and_send(pid, publisher=log, policy=ON, db_path=seeded_db_path)

    assert outcome.outcome == "held" and outcome.detail == reason
    assert ProposalStore(seeded_db_path).get(pid).status == "pending"


def test_a_decided_proposal_is_not_waiting(seeded_db_path, log):
    pid = _pending(seeded_db_path)
    approve_and_send(pid, approver_id="sharon.silva", publisher=log, policy=OFF, db_path=seeded_db_path)

    assert "already" in explain_hold(pid, policy=ON, publisher=log, db_path=seeded_db_path).lower()
