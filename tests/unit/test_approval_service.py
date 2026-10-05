"""PM-13: the approval gate wired to the proposal spine.

The scheduled morning job only PROPOSES: it creates one pending proposal and
sends nothing. A person approves, rejects, or edits-then-approves through the
service; only an approved proposal is executed, through the Teams adapter, and
logged. The service -- not any card or script in front of it -- is where this
is enforced, so it is tested directly, including every way around it.

Acceptance test (the audit trail answers "who approved this entry, when, and
what did the agent originally propose?") is test_the_audit_trail_answers_...
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from p1.adapters.teams_publisher import TeamsPublisher
from p1.adapters.teams_publisher_mock import LogPublisher
from p1.adapters.teams_publisher_power_automate import PowerAutomateTeamsPublisher
from spine.approval.proposals import (
    APPLIED,
    APPROVED,
    PENDING,
    REJECTED,
    ProposalNotFoundError,
    ProposalStore,
)
from spine.approval.write_guard import WriteRefusedError, guarded_send

from pm.approval.audit import audit_trail, describe
from pm.approval.service import (
    ApprovalPolicy,
    approve_and_send,
    list_pending_approvals,
    reject,
    send_approved,
)
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import GENERATED, run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

WED = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # Wed 08:00 in Colombo
THU = datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc)
SHARON = "sharon.silva"
TEST_CHANNEL = "19:test-channel@thread.tacv2"
POLICY = ApprovalPolicy(approver_ids=frozenset({SHARON, "noah.becker"}), allowlisted_channel_ids=[TEST_CHANNEL])


def _config(**overrides):
    values = {
        "channel_id": CHANNEL_ID, "timezone": "Asia/Colombo", "working_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "morning_brief_time": time(8, 0), "end_of_day_time": time(17, 0),
    }
    return ProjectScheduleConfig(**{**values, **overrides})


def _propose(db, moment=WED, **config_overrides):
    result = run_morning_brief_job(_config(**config_overrides), ScriptedGateway(), moment=moment, db_path=db)
    assert result.status == GENERATED and result.proposal_id
    return result


@pytest.fixture()
def log(tmp_path):
    return LogPublisher(tmp_path / "log.jsonl")


def _raw(db, sql, *args):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# --- the job only proposes --------------------------------------------------------


def test_the_morning_job_creates_one_pending_proposal_and_sends_nothing(seeded_db_path, log):
    result = _propose(seeded_db_path)

    proposal = ProposalStore(seeded_db_path).get(result.proposal_id)
    assert proposal.status == PENDING and proposal.type == "morning_brief_publish"
    assert proposal.payload["content"].startswith("Morning brief — 2026-09-16\n\n")
    assert proposal.payload["target_channel"] == CHANNEL_ID
    assert _raw(seeded_db_path, "SELECT * FROM write_log") == []
    assert log.read_log() == []


def test_the_proposal_records_what_the_agent_proposed_and_its_sources(seeded_db_path):
    result = _propose(seeded_db_path)

    proposal = ProposalStore(seeded_db_path).get(result.proposal_id)
    assert proposal.original_model_output["content"] == proposal.payload["content"]
    assert proposal.original_model_output["lines"]  # the grounded lines, each with its reference
    assert all(line["reference_id"] for line in proposal.original_model_output["lines"])
    assert "item:PM-001" in proposal.source_refs


def test_one_proposal_per_target_channel_per_day(seeded_db_path):
    first = _propose(seeded_db_path)
    again = _propose(seeded_db_path)
    next_day = _propose(seeded_db_path, moment=THU)

    assert again.proposal_id == first.proposal_id and again.delivery_status == "already_proposed"
    assert next_day.proposal_id != first.proposal_id
    assert first.delivery_status == "proposed"


def test_a_label_and_target_channel_go_into_the_proposal(seeded_db_path):
    result = _propose(seeded_db_path, publish_channel_id=TEST_CHANNEL, message_label="[sample data] ")

    payload = ProposalStore(seeded_db_path).get(result.proposal_id).payload
    assert payload["target_channel"] == TEST_CHANNEL and payload["content"].startswith("[sample data] Morning brief")


def test_pending_approvals_lists_what_awaits_a_decision(seeded_db_path):
    result = _propose(seeded_db_path)

    (pending,) = list_pending_approvals(db_path=seeded_db_path)

    assert pending.proposal_id == result.proposal_id and pending.target_channel == CHANNEL_ID
    assert pending.local_date == "2026-09-16" and pending.content.startswith("Morning brief")


# --- approve ----------------------------------------------------------------------


def test_approving_executes_through_the_adapter_and_logs_it(seeded_db_path, log):
    result = _propose(seeded_db_path)

    outcome = approve_and_send(result.proposal_id, approver_id=SHARON, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "sent"
    proposal = ProposalStore(seeded_db_path).get(result.proposal_id)
    assert proposal.status == APPLIED and proposal.approver_id == SHARON
    (row,) = log.read_log()
    assert row["target"] == CHANNEL_ID and row["content"] == proposal.payload["content"]
    (write,) = _raw(seeded_db_path, "SELECT * FROM write_log WHERE proposal_id = ?", result.proposal_id)
    assert write["status"] == "sent" and write["target"] == CHANNEL_ID


def test_a_second_approval_does_not_send_a_second_time(seeded_db_path, log):
    result = _propose(seeded_db_path)
    approve_and_send(result.proposal_id, approver_id=SHARON, publisher=log, policy=POLICY, db_path=seeded_db_path)

    again = approve_and_send(result.proposal_id, approver_id=SHARON, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert again.outcome == "refused" and len(log.read_log()) == 1


# --- reject -----------------------------------------------------------------------


def test_rejecting_sends_nothing_and_cannot_be_sent_later(seeded_db_path, log):
    result = _propose(seeded_db_path)

    outcome = reject(result.proposal_id, approver_id=SHARON, policy=POLICY, db_path=seeded_db_path)
    later = send_approved(result.proposal_id, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "rejected" and later.outcome == "refused"
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == REJECTED
    assert log.read_log() == []


def test_a_rejected_proposal_cannot_then_be_approved(seeded_db_path, log):
    result = _propose(seeded_db_path)
    reject(result.proposal_id, approver_id=SHARON, policy=POLICY, db_path=seeded_db_path)

    outcome = approve_and_send(result.proposal_id, approver_id=SHARON, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "refused" and log.read_log() == []


# --- edit, then approve ---------------------------------------------------------------


def test_edit_then_approve_sends_the_edited_text_and_keeps_the_original(seeded_db_path, log):
    result = _propose(seeded_db_path)
    original = ProposalStore(seeded_db_path).get(result.proposal_id).payload["content"]
    edited = original.replace("Morning brief", "Morning brief (reviewed)", 1)

    outcome = approve_and_send(
        result.proposal_id, approver_id=SHARON, edited_content=edited, publisher=log, policy=POLICY, db_path=seeded_db_path
    )

    assert outcome.outcome == "sent"
    (row,) = log.read_log()
    assert row["content"] == edited != original
    proposal = ProposalStore(seeded_db_path).get(result.proposal_id)
    assert proposal.payload["content"] == edited and proposal.original_model_output["content"] == original


def test_an_unchanged_edit_is_just_an_approval(seeded_db_path, log):
    result = _propose(seeded_db_path)
    content = ProposalStore(seeded_db_path).get(result.proposal_id).payload["content"]

    approve_and_send(result.proposal_id, approver_id=SHARON, edited_content=content, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert audit_trail(result.proposal_id, db_path=seeded_db_path).edited is False


@pytest.mark.parametrize("bad", ["", "   \n  "])
def test_an_empty_edit_is_refused_and_the_proposal_stays_pending(seeded_db_path, log, bad):
    result = _propose(seeded_db_path)

    outcome = approve_and_send(result.proposal_id, approver_id=SHARON, edited_content=bad, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "refused"
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING and log.read_log() == []


# --- who may decide -------------------------------------------------------------------


def test_someone_who_is_not_an_approver_cannot_approve_or_reject(seeded_db_path, log):
    result = _propose(seeded_db_path)

    approved = approve_and_send(result.proposal_id, approver_id="mallory", publisher=log, policy=POLICY, db_path=seeded_db_path)
    rejected = reject(result.proposal_id, approver_id="mallory", policy=POLICY, db_path=seeded_db_path)

    assert approved.outcome == rejected.outcome == "refused"
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING and log.read_log() == []
    denied = _raw(seeded_db_path, "SELECT actor, action FROM audit WHERE action = 'proposal.denied'")
    assert [d["actor"] for d in denied] == ["mallory", "mallory"]


def test_with_no_approvers_configured_nobody_can_approve(seeded_db_path, log):
    result = _propose(seeded_db_path)

    outcome = approve_and_send(
        result.proposal_id, approver_id=SHARON, publisher=log, policy=ApprovalPolicy(approver_ids=frozenset()), db_path=seeded_db_path
    )

    assert outcome.outcome == "refused" and log.read_log() == []


def test_a_blank_approver_is_refused(seeded_db_path, log):
    result = _propose(seeded_db_path)

    outcome = approve_and_send(result.proposal_id, approver_id="  ", publisher=log, policy=ApprovalPolicy(approver_ids=frozenset({""})), db_path=seeded_db_path)

    assert outcome.outcome == "refused"


# --- no way around the gate -------------------------------------------------------------


def test_nothing_can_be_executed_without_an_approved_proposal(seeded_db_path, log):
    result = _propose(seeded_db_path)

    outcome = send_approved(result.proposal_id, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "refused" and log.read_log() == []
    with pytest.raises(WriteRefusedError):
        guarded_send(result.proposal_id, action_type="channel_post", target=CHANNEL_ID,
                     send_fn=lambda: log.post_channel_message(CHANNEL_ID, "bypass"), db_path=seeded_db_path)
    assert log.read_log() == []


def test_an_unknown_proposal_id_is_refused_not_an_error(seeded_db_path, log):
    assert send_approved("no-such-id", publisher=log, policy=POLICY, db_path=seeded_db_path).outcome == "refused"
    assert approve_and_send("no-such-id", approver_id=SHARON, publisher=log, policy=POLICY, db_path=seeded_db_path).outcome == "refused"


# --- failure and retry ----------------------------------------------------------------------


class _Flaky(TeamsPublisher):
    def __init__(self):
        self.fail = True
        self.posts = []

    def post_channel_message(self, channel_id, content):
        if self.fail:
            raise ConnectionError("flow unreachable")
        self.posts.append((channel_id, content))
        return {"ok": True}

    def post_direct_message(self, member_id, content):
        raise AssertionError("never used")


def test_a_failed_send_leaves_it_approved_and_a_retry_sends_it_once(seeded_db_path):
    result = _propose(seeded_db_path, publish_channel_id=TEST_CHANNEL)  # _Flaky is a real-kind publisher: allowlisted target
    publisher = _Flaky()

    failed = approve_and_send(result.proposal_id, approver_id=SHARON, publisher=publisher, policy=POLICY, db_path=seeded_db_path)

    assert failed.outcome == "send_failed"
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == APPROVED
    assert _raw(seeded_db_path, "SELECT status FROM write_log")[0]["status"] == "send_failed"

    publisher.fail = False
    retried = send_approved(result.proposal_id, publisher=publisher, policy=POLICY, db_path=seeded_db_path)
    again = send_approved(result.proposal_id, publisher=publisher, policy=POLICY, db_path=seeded_db_path)

    assert (retried.outcome, again.outcome) == ("sent", "refused") and len(publisher.posts) == 1


# --- a real publisher: scope, and the wire ---------------------------------------------------


class _Flow:
    def __init__(self):
        self.requests = []
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                flow.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/flow"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def flow():
    f = _Flow()
    yield f
    f.close()


def test_a_real_publisher_is_refused_for_a_channel_off_the_allowlist_and_the_proposal_stays_pending(seeded_db_path, flow):
    result = _propose(seeded_db_path)  # targets the seeded, non-allowlisted channel

    outcome = approve_and_send(
        result.proposal_id, approver_id=SHARON, publisher=PowerAutomateTeamsPublisher(flow.url), policy=POLICY, db_path=seeded_db_path
    )

    assert outcome.outcome == "refused" and "allowlist" in outcome.detail
    assert ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING and flow.requests == []


def test_an_approved_post_to_an_allowlisted_channel_reaches_the_flow_once(seeded_db_path, flow):
    result = _propose(seeded_db_path, publish_channel_id=TEST_CHANNEL)

    outcome = approve_and_send(
        result.proposal_id, approver_id=SHARON, publisher=PowerAutomateTeamsPublisher(flow.url), policy=POLICY, db_path=seeded_db_path
    )

    assert outcome.outcome == "sent"
    (request,) = flow.requests
    assert request["action_type"] == "channel_post" and request["target"] == TEST_CHANNEL
    assert request["content"] == ProposalStore(seeded_db_path).get(result.proposal_id).payload["content"]


# --- the audit trail ---------------------------------------------------------------------------


def test_the_audit_trail_answers_who_approved_when_and_what_was_originally_proposed(seeded_db_path, log):
    """PM-13's acceptance test, literal: who approved this entry, when, and
    what did the agent originally propose?"""
    result = _propose(seeded_db_path)
    original = ProposalStore(seeded_db_path).get(result.proposal_id).payload["content"]
    edited = original + "\n\nNote from the PM: standup moved to 09:30."
    before = datetime.now(timezone.utc)

    approve_and_send(result.proposal_id, approver_id=SHARON, edited_content=edited, publisher=log, policy=POLICY, db_path=seeded_db_path)
    after = datetime.now(timezone.utc)

    trail = audit_trail(result.proposal_id, db_path=seeded_db_path)

    assert trail.approver_id == SHARON  # who approved
    assert before <= datetime.fromisoformat(trail.decided_at) <= after  # when
    assert trail.original_proposal["content"] == original  # what the agent originally proposed
    assert trail.final_proposal["content"] == edited and trail.edited is True  # and what was applied
    assert trail.status == APPLIED and trail.sent["status"] == "sent" and trail.sent["target"] == CHANNEL_ID
    assert datetime.fromisoformat(trail.sent["created_at"]).tzinfo is not None  # same ISO UTC form as every timestamp
    assert [e["action"] for e in trail.events] == [
        "proposal.created", "proposal.edited", "proposal.approved", "proposal.sent",
    ]
    assert {e["actor"] for e in trail.events if e["action"] != "proposal.created"} == {SHARON}
    assert trail.events[0]["actor"] == "agent"


def test_the_trail_agrees_with_the_raw_rows(seeded_db_path, log):
    result = _propose(seeded_db_path)
    approve_and_send(result.proposal_id, approver_id="noah.becker", publisher=log, policy=POLICY, db_path=seeded_db_path)

    trail = audit_trail(result.proposal_id, db_path=seeded_db_path)

    (row,) = _raw(seeded_db_path, "SELECT * FROM proposals WHERE id = ?", result.proposal_id)
    assert trail.approver_id == row["approver_id"] == "noah.becker" and trail.decided_at == row["decided_at"]
    assert trail.original_proposal == json.loads(row["original_model_output"])
    assert trail.final_proposal == json.loads(row["payload"])


def test_an_edit_never_overwrites_the_original(seeded_db_path, log):
    result = _propose(seeded_db_path)
    (before,) = _raw(seeded_db_path, "SELECT original_model_output FROM proposals")

    approve_and_send(result.proposal_id, approver_id=SHARON, edited_content="Completely different text.", publisher=log, policy=POLICY, db_path=seeded_db_path)

    (after,) = _raw(seeded_db_path, "SELECT original_model_output FROM proposals")
    assert after == before


def test_a_rejection_is_in_the_trail_too(seeded_db_path):
    result = _propose(seeded_db_path)
    reject(result.proposal_id, approver_id=SHARON, reason="figures look stale", policy=POLICY, db_path=seeded_db_path)

    trail = audit_trail(result.proposal_id, db_path=seeded_db_path)

    assert trail.status == REJECTED and trail.approver_id == SHARON and trail.decided_at and trail.sent is None
    rejected = next(e for e in trail.events if e["action"] == "proposal.rejected")
    assert rejected["actor"] == SHARON and rejected["details"]["reason"] == "figures look stale"


def test_a_pending_proposal_has_no_approver_yet(seeded_db_path):
    result = _propose(seeded_db_path)

    trail = audit_trail(result.proposal_id, db_path=seeded_db_path)

    assert trail.status == PENDING and trail.approver_id is None and trail.decided_at is None


def test_describe_answers_in_a_sentence(seeded_db_path, log):
    result = _propose(seeded_db_path)
    approve_and_send(result.proposal_id, approver_id=SHARON, edited_content="Edited text for the team.", publisher=log, policy=POLICY, db_path=seeded_db_path)

    text = describe(audit_trail(result.proposal_id, db_path=seeded_db_path))

    assert "approved by sharon.silva" in text.lower() and "edited" in text.lower()
    assert "Morning brief — 2026-09-16" in text  # the agent's original, quoted
    assert "Edited text for the team." in text  # what was applied


def test_an_unknown_proposal_has_no_trail(seeded_db_path):
    with pytest.raises(ProposalNotFoundError):
        audit_trail("no-such-id", db_path=seeded_db_path)
