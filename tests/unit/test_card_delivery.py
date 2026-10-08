"""Each pending proposal gets its card in Teams ONCE, however often a flow asks.

A flow that runs on a timer must not post the same unanswered cards every cycle. /claim_new_approvals hands out only the proposals no card has been
sent for, and records that it did, in the transaction that decided it was due. An undecided one is handed out again only after the resend window.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from spine.approval.proposals import ProposalStore

from pm.api import copilot_studio_api as api
from pm.approval import cards, service
from pm.approval.audit import audit_trail
from pm.approval.card_delivery import CARD_SENT, claim_new_approvals

APPROVERS = service.ApprovalPolicy(approver_ids=frozenset({"sharon"}))
NOW = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)


def make(db, n: int) -> str:
    return ProposalStore(db).create(
        type="morning_brief_publish", payload={"target_channel": f"19:c{n}@thread.tacv2", "local_date": "2026-10-08", "content": f"Brief {n}"},
        original_model_output={"content": f"Brief {n}"}, source_refs=[], idempotency_key=f"brief-{n}",
    ).id


def ids(approvals) -> list[str]:
    return [a.proposal_id for a in approvals]


def test_a_first_call_hands_out_every_pending_proposal_oldest_first_and_a_second_hands_out_none(seeded_db_path):
    first, second = make(seeded_db_path, 1), make(seeded_db_path, 2)

    one = claim_new_approvals(db_path=seeded_db_path, now=NOW)
    two = claim_new_approvals(db_path=seeded_db_path, now=NOW + timedelta(minutes=5))

    assert ids(one) == [first, second] and two == []


def test_only_the_proposal_made_since_the_last_call_comes_next(seeded_db_path):
    make(seeded_db_path, 1)
    claim_new_approvals(db_path=seeded_db_path, now=NOW)
    later = make(seeded_db_path, 2)

    assert ids(claim_new_approvals(db_path=seeded_db_path, now=NOW + timedelta(minutes=5))) == [later]


def test_a_decided_proposal_is_never_handed_out(seeded_db_path):
    decided, waiting = make(seeded_db_path, 1), make(seeded_db_path, 2)
    service.reject(decided, approver_id="sharon", policy=APPROVERS, db_path=seeded_db_path)

    assert ids(claim_new_approvals(db_path=seeded_db_path, now=NOW)) == [waiting]
    assert CARD_SENT not in [e["action"] for e in audit_trail(decided, db_path=seeded_db_path).events]  # and nothing is recorded as sent for it


def test_handing_out_is_recorded_in_the_audit_and_changes_nothing_else(seeded_db_path):
    pid = make(seeded_db_path, 1)

    claim_new_approvals(db_path=seeded_db_path, now=NOW)

    trail = audit_trail(pid, db_path=seeded_db_path)
    assert [(e["actor"], e["action"], e["details"]) for e in trail.events] == [("agent", CARD_SENT, {"kind": "first"})]
    assert trail.status == "pending" and trail.approver_id is None


def test_an_undecided_proposal_is_handed_out_again_only_after_the_resend_window(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_CARD_RESEND_HOURS", "24")
    pid = make(seeded_db_path, 1)
    claim_new_approvals(db_path=seeded_db_path, now=NOW)

    assert claim_new_approvals(db_path=seeded_db_path, now=NOW + timedelta(hours=23, minutes=59)) == []
    again = claim_new_approvals(db_path=seeded_db_path, now=NOW + timedelta(hours=24))

    assert ids(again) == [pid]
    assert [e["details"] for e in audit_trail(pid, db_path=seeded_db_path).events] == [{"kind": "first"}, {"kind": "reminder"}]
    assert claim_new_approvals(db_path=seeded_db_path, now=NOW + timedelta(hours=25)) == []  # the clock restarts from the reminder


def test_reminders_can_be_turned_off(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_CARD_RESEND_HOURS", "0")
    make(seeded_db_path, 1)
    claim_new_approvals(db_path=seeded_db_path, now=NOW)

    assert claim_new_approvals(db_path=seeded_db_path, now=NOW + timedelta(days=30)) == []


def test_callers_at_the_same_moment_never_get_the_same_proposal_and_none_fails(seeded_db_path):
    """Several flow runs can ask in the same instant. Each proposal must go to exactly one of them, and nobody may be turned away."""
    seen: list[str] = []
    errors: list[Exception] = []
    for round_number in range(5):
        for n in range(1, 9):
            make(seeded_db_path, round_number * 10 + n)
        got: list[list[str]] = []
        gate = threading.Barrier(6)

        def call(gate=gate, got=got):
            gate.wait()
            try:
                got.append(ids(claim_new_approvals(db_path=seeded_db_path, now=NOW)))
            except Exception as exc:  # noqa: BLE001 - recorded and asserted on below
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        seen += [i for part in got for i in part]

    assert errors == []
    assert len(seen) == 40 and len(set(seen)) == 40  # every one of the forty went to exactly one caller


@pytest.fixture()
def client(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_COPILOT_API_KEY", "k")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon")
    with TestClient(api.app) as c:
        yield c


def test_the_api_action_needs_the_key_and_hands_out_cards_once(seeded_db_path, client):
    make(seeded_db_path, 1)

    assert client.post("/claim_new_approvals", json={}).status_code == 401
    first = client.post("/claim_new_approvals", json={}, headers={"X-API-Key": "k"}).json()["approvals"]
    second = client.post("/claim_new_approvals", json={}, headers={"X-API-Key": "k"}).json()["approvals"]

    assert len(first) == 1 and first[0]["card"]["type"] == "AdaptiveCard" and [a["title"] for a in first[0]["card"]["actions"]] == ["Approve", "Reject"]
    assert second == []


def test_listing_every_pending_proposal_still_shows_the_ones_already_handed_out(seeded_db_path, client):
    make(seeded_db_path, 1)
    client.post("/claim_new_approvals", json={}, headers={"X-API-Key": "k"})

    listed = client.post("/list_pending_approvals", json={}, headers={"X-API-Key": "k"}).json()["approvals"]

    assert len(listed) == 1  # the plain list is unchanged: it answers "what is waiting", not "what is new"


def test_the_handler_matches_the_listing_shape(seeded_db_path):
    pid = make(seeded_db_path, 1)

    claimed = cards.handle_claim_new({}, db_path=seeded_db_path)["approvals"]

    assert [a["proposal_id"] for a in claimed] == [pid] and set(claimed[0]) == {"proposal_id", "type", "target_channel", "local_date", "created_at", "summary", "card"}
