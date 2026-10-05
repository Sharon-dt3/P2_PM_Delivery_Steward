"""PM-13: Teams cards are the human surface; enforcement stays in the service.

The Adaptive Card is what a person sees and clicks in Teams (Copilot Studio
renders it). Nothing about it can grant authority: the handler takes WHO is
acting from the platform's authenticated identity, never from anything the
card submitted, and calls the same service functions as the CLI -- so a card
click and a command-line approval leave identical audit records.

Not built or exercised: the Copilot Studio agent itself (no tenant here), same
as P1's CHN-25. What is real and tested is the card JSON and the backend
handler its Action.Submit data is checked against.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import PENDING, ProposalStore

from pm.approval.audit import audit_trail
from pm.approval.cards import (
    decision_card,
    handle_card_action,
    handle_list_pending,
    pending_brief_card,
)
from pm.approval.service import ApprovalPolicy, list_pending_approvals
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

WED = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)
POLICY = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), allowlisted_channel_ids=[])


@pytest.fixture()
def proposal(seeded_db_path):
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    return run_morning_brief_job(config, ScriptedGateway(), moment=WED, db_path=seeded_db_path).proposal_id


@pytest.fixture()
def log(tmp_path):
    return LogPublisher(tmp_path / "log.jsonl")


def _card(db):
    (pending,) = list_pending_approvals(db_path=db)
    return pending_brief_card(pending)


def _elements(card, kind):
    return [e for e in card["body"] if e["type"] == kind]


def _submits(card):
    return {a["title"]: a for a in card["actions"] if a["type"] == "Action.Submit"}


# --- the card --------------------------------------------------------------------------


def test_the_card_is_an_adaptive_card_that_shows_the_proposal(seeded_db_path, proposal):
    card = _card(seeded_db_path)
    content = ProposalStore(seeded_db_path).get(proposal).payload["content"]

    assert card["type"] == "AdaptiveCard" and card["version"] == "1.5"
    text = json.dumps(card)
    assert CHANNEL_ID in text and "2026-09-16" in text
    (edit_box,) = _elements(card, "Input.Text")
    assert edit_box["id"] == "edited_content" and edit_box["isMultiline"] is True and edit_box["value"] == content


def test_the_card_offers_approve_and_reject_and_reject_needs_no_input(seeded_db_path, proposal):
    submits = _submits(_card(seeded_db_path))

    assert set(submits) == {"Approve", "Reject"}
    assert submits["Approve"]["data"] == {"action": "approve", "proposal_id": proposal}
    assert submits["Reject"]["data"] == {"action": "reject", "proposal_id": proposal}
    assert submits["Reject"].get("associatedInputs") == "none"


def test_the_card_carries_no_approver_field(seeded_db_path, proposal):
    assert "approver" not in json.dumps(_card(seeded_db_path)).lower()


def test_the_card_is_plain_json(seeded_db_path, proposal):
    card = _card(seeded_db_path)

    assert json.loads(json.dumps(card)) == card


# --- the handler ----------------------------------------------------------------------------


def test_an_approve_click_sends_and_is_attributed_to_the_authenticated_user(seeded_db_path, proposal, log):
    data = _submits(_card(seeded_db_path))["Approve"]["data"]

    result = handle_card_action(
        {**data, "edited_content": ProposalStore(seeded_db_path).get(proposal).payload["content"]},
        authenticated_user_id="sharon.silva", publisher=log, policy=POLICY, db_path=seeded_db_path,
    )

    assert result["outcome"] == "sent" and result["proposal_id"] == proposal
    assert audit_trail(proposal, db_path=seeded_db_path).approver_id == "sharon.silva"
    assert len(log.read_log()) == 1


def test_editing_the_text_in_the_card_then_approving_applies_the_edit(seeded_db_path, proposal, log):
    data = _submits(_card(seeded_db_path))["Approve"]["data"]

    result = handle_card_action(
        {**data, "edited_content": "Shorter brief for today."},
        authenticated_user_id="sharon.silva", publisher=log, policy=POLICY, db_path=seeded_db_path,
    )

    assert result["outcome"] == "sent"
    assert log.read_log()[0]["content"] == "Shorter brief for today."
    trail = audit_trail(proposal, db_path=seeded_db_path)
    assert trail.edited and trail.original_proposal["content"].startswith("Morning brief")


def test_a_reject_click_rejects(seeded_db_path, proposal, log):
    data = _submits(_card(seeded_db_path))["Reject"]["data"]

    result = handle_card_action(data, authenticated_user_id="sharon.silva", publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert result["outcome"] == "rejected" and log.read_log() == []
    assert ProposalStore(seeded_db_path).get(proposal).status != PENDING


def test_an_approver_name_inside_the_submitted_data_is_ignored(seeded_db_path, proposal, log):
    forged = {"action": "approve", "proposal_id": proposal, "approver_id": "sharon.silva", "approver": "sharon.silva"}

    result = handle_card_action(forged, authenticated_user_id="mallory", publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert result["outcome"] == "refused" and log.read_log() == []
    assert ProposalStore(seeded_db_path).get(proposal).status == PENDING


def test_a_request_with_no_authenticated_user_is_refused(seeded_db_path, proposal, log):
    for user in (None, "", "   "):
        result = handle_card_action(
            {"action": "approve", "proposal_id": proposal}, authenticated_user_id=user,
            publisher=log, policy=POLICY, db_path=seeded_db_path,
        )
        assert result["outcome"] == "refused", user
    assert log.read_log() == []


@pytest.mark.parametrize("request_", [{}, {"action": "approve"}, {"proposal_id": "x"}, {"action": "delete", "proposal_id": "x"}])
def test_malformed_or_unknown_actions_are_refused_not_raised(seeded_db_path, log, request_):
    result = handle_card_action(request_, authenticated_user_id="sharon.silva", publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert result["outcome"] == "refused" and log.read_log() == []


def test_a_card_click_and_a_direct_service_call_leave_the_same_audit_records(seeded_db_path, tmp_path, log):
    """The gate is in the service: both routes must be indistinguishable."""
    from pm.approval.service import approve_and_send

    def run(db, via_card):
        config = ProjectScheduleConfig(
            channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
            morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
        )
        pid = run_morning_brief_job(config, ScriptedGateway(), moment=WED, db_path=db).proposal_id
        publisher = LogPublisher(tmp_path / f"{'card' if via_card else 'direct'}.jsonl")
        if via_card:
            handle_card_action({"action": "approve", "proposal_id": pid}, authenticated_user_id="sharon.silva",
                               publisher=publisher, policy=POLICY, db_path=db)
        else:
            approve_and_send(pid, approver_id="sharon.silva", publisher=publisher, policy=POLICY, db_path=db)
        trail = audit_trail(pid, db_path=db)
        return [(e["actor"], e["action"]) for e in trail.events], trail.status, trail.final_proposal

    from spine.storage.db import run_migrations

    from pm.seed.build import build_seed
    from pm.storage.db import MIGRATIONS_DIR, get_connection

    other = tmp_path / "second.db"
    run_migrations(other, MIGRATIONS_DIR)
    conn = get_connection(other)
    try:
        build_seed(conn)
    finally:
        conn.close()

    assert run(seeded_db_path, True) == run(other, False)


def test_the_handlers_listing_returns_each_pending_item_with_its_card(seeded_db_path, proposal):
    response = handle_list_pending({}, db_path=seeded_db_path)

    (item,) = response["approvals"]
    assert item["proposal_id"] == proposal and item["card"]["type"] == "AdaptiveCard"
    assert item["card"]["actions"][0]["data"]["proposal_id"] == proposal


def test_the_decision_card_shows_who_decided_when_and_what_was_proposed(seeded_db_path, proposal, log):
    from pm.approval.service import approve_and_send

    approve_and_send(proposal, approver_id="sharon.silva", edited_content="Edited text.", publisher=log, policy=POLICY, db_path=seeded_db_path)

    card = decision_card(audit_trail(proposal, db_path=seeded_db_path))

    text = json.dumps(card, ensure_ascii=False)
    assert card["type"] == "AdaptiveCard" and "sharon.silva" in text and "Edited text." in text
    assert "Morning brief — 2026-09-16" in text  # the agent's original, still shown
