"""PM-28 meets PM-26: the two batches made from P1's outcome record are shown in Teams for what they are.

A batch is not a morning brief and not a single risk-log gap entry. The approval list used to describe every proposal it did not
recognise as a risk-log entry, so a batch would have appeared as "Proposed risk log entry for ? (?)" with nothing in the card. Each
batch now says what it holds, from which channel and day, and lists its items with the message that justifies each.
"""

from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from spine.approval.proposals import REJECTED, ProposalStore

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.tracker import TrackerMock
from pm.api import copilot_studio_api as api
from pm.approval import cards, service
from pm.channel.batches import (
    CHANNEL_RISK_PROPOSAL_TYPE,
    CHANNEL_TRACKER_PROPOSAL_TYPE,
    TrackerView,
    consume,
)

CHANNEL = "19:proj-gamma@thread.tacv2"
RECORD = {
    "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": "2026-09-18", "allowlisted": True,
    "roster": [], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "decisions": [], "questions": [],
    "updates": [{"message_id": "m1", "text": "PM-016 export bug is fixed and in review.", "quote": None}],
    "blockers": [
        {"message_id": "m4", "text": "PM-014 is still blocked on the staging migration.", "quote": None},
        {"message_id": "m6", "text": "The adapter payload shape is undocumented.", "quote": None},
    ],
}


@pytest.fixture()
def batches(seeded_db_path, tmp_path):
    path = tmp_path / "2026-09-18.json"
    path.write_text(json.dumps(RECORD), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=seeded_db_path), RiskLogMock(db_path=seeded_db_path))
    result = consume(path, view, db_path=seeded_db_path)
    return {"db": seeded_db_path, "tracker": result.tracker.proposal_id, "risk": result.risk.proposal_id}


def _pending(db):
    return {p.proposal_id: p for p in service.list_pending_approvals(db_path=db)}


def test_each_batch_says_what_it_holds_and_where_it_came_from(batches):
    pending = _pending(batches["db"])

    assert pending[batches["tracker"]].summary == "Tracker changes from Project Gamma (2026-09-18): 3 items"
    assert pending[batches["risk"]].summary == "Risk-log entries from Project Gamma (2026-09-18): 2 items"
    assert "?" not in pending[batches["tracker"]].summary + pending[batches["risk"]].summary


def test_each_item_is_listed_with_the_message_that_justifies_it(batches):
    tracker = _pending(batches["db"])[batches["tracker"]].content
    risk = _pending(batches["db"])[batches["risk"]].content

    assert "Comment on PM-016: " in tracker and "PM-016 export bug is fixed and in review." in tracker
    assert "Comment on PM-014: " in tracker and "New item (blocked, nobody assigned): The adapter payload shape is undocumented." in tracker
    assert tracker.count("Project Gamma message m") == 3 and "m4" in tracker and "m6" in tracker
    assert "New risk entry for PM-014" in risk and "suggested owner olivia.dupree" in risk and "New risk entry for no item" in risk


def test_a_reworded_item_says_what_it_replaces(batches, tmp_path):
    reworded = dict(RECORD, blockers=[dict(RECORD["blockers"][1], text="The adapter payload shape is still undocumented after a second ask.")])
    path = tmp_path / "again.json"
    path.write_text(json.dumps(reworded), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=batches["db"]), RiskLogMock(db_path=batches["db"]))

    again = consume(path, view, db_path=batches["db"])

    content = _pending(batches["db"])[again.risk.proposal_id].content
    assert "replaces an earlier wording" in content and "The adapter payload shape is undocumented." in content


def test_the_tracker_batch_card_shows_the_whole_list_and_offers_approve_and_reject_with_no_inputs(batches):
    listed = cards.handle_list_pending({}, db_path=batches["db"])["approvals"]

    card = {a["proposal_id"]: a for a in listed}[batches["tracker"]]["card"]

    assert [a["title"] for a in card["actions"]] == ["Approve", "Reject"]
    assert not [e for e in card["body"] if e.get("type", "").startswith("Input.")]  # nothing to fill in: approved whole or rejected
    assert "Project Gamma" in json.dumps(card) and "Approving writes these to the tracker" in json.dumps(card)


def test_a_clipped_title_is_shown_with_the_whole_line_so_the_approver_reads_what_was_said(seeded_db_path, tmp_path):
    long_line = ("The adapter on the copilot studio side requires a payload shape that has not been documented, and nobody has said "
                 "when the vendor will send it, so the whole integration is waiting on an answer we have asked for twice already.")
    record = dict(RECORD, blockers=[{"message_id": "m9", "text": long_line, "quote": None}], updates=[])
    path = tmp_path / "long.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=seeded_db_path), RiskLogMock(db_path=seeded_db_path))
    result = consume(path, view, db_path=seeded_db_path)

    pending = {p.proposal_id: p for p in service.list_pending_approvals(db_path=seeded_db_path)}

    for proposal_id in (result.tracker.proposal_id, result.risk.proposal_id):
        content = pending[proposal_id].content
        assert "..." in content.splitlines()[0] and f"the whole line: {long_line}" in content  # clipped title, then everything that was said


def test_the_risk_batch_card_offers_a_severity_to_pick_approve_and_reject(batches):
    listed = cards.handle_list_pending({}, db_path=batches["db"])["approvals"]

    card = {a["proposal_id"]: a for a in listed}[batches["risk"]]["card"]

    assert [a["title"] for a in card["actions"]] == ["Approve", "Reject"]
    (picker,) = [e for e in card["body"] if e.get("type") == "Input.ChoiceSet"]
    assert picker["id"] == "severity" and picker["value"] == "medium" and [c["value"] for c in picker["choices"]] == ["low", "medium", "high"]
    assert "Project Gamma" in json.dumps(card) and "Approving writes this to the risk log" in json.dumps(card)
    assert not [e for e in card["body"] if e.get("type") == "Input.Text"]  # no edit box on a risk entry
    assert card["actions"][1]["associatedInputs"] == "none"  # rejecting needs no severity


def test_both_batches_are_executable_writes_and_neither_is_a_message(batches):
    assert {CHANNEL_RISK_PROPOSAL_TYPE, CHANNEL_TRACKER_PROPOSAL_TYPE} <= service.EXECUTABLE_TYPES
    assert not {CHANNEL_RISK_PROPOSAL_TYPE, CHANNEL_TRACKER_PROPOSAL_TYPE} & service.MESSAGE_TYPES  # never posted, never auto-approved


def test_rejecting_a_batch_from_teams_leaves_the_same_audit_record_as_the_command_line(batches, tmp_path, monkeypatch):
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
    fallback = tmp_path / "fallback.db"
    shutil.copy(batches["db"], fallback)
    service.reject(batches["risk"], approver_id="sharon.silva", reason="not a risk", policy=policy, db_path=fallback)
    monkeypatch.setenv("PM_DB_PATH", str(batches["db"]))
    monkeypatch.setenv("PM_COPILOT_API_KEY", "k")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")

    with TestClient(api.app) as client:
        result = client.post("/card_action", json={"action": "reject", "proposal_id": batches["risk"], "reason": "not a risk"},
                             headers={"X-API-Key": "k", "X-Authenticated-User": "sharon.silva"}).json()

    assert result["outcome"] == service.REJECTED_OUTCOME
    assert ProposalStore(batches["db"]).get(batches["risk"]).status == REJECTED

    def rows(db):
        import sqlite3

        conn = sqlite3.connect(db)
        out = conn.execute("SELECT actor, action, entity_type, details FROM audit WHERE entity_id = ? ORDER BY id", (batches["risk"],)).fetchall()
        conn.close()
        return out

    assert rows(batches["db"]) == rows(fallback) and any(r[1] == "proposal.rejected" for r in rows(fallback))


def test_the_old_risk_log_gap_proposals_are_still_described_as_before(seeded_db_path):
    store = ProposalStore(seeded_db_path)
    store.create(type="risk_log_entry", payload={"item_id": "PM-014", "blocker_ref": "item:PM-014"}, original_model_output={}, source_refs=[],
                 idempotency_key="old-risk-gap")

    (pending,) = service.list_pending_approvals(db_path=seeded_db_path)

    assert pending.summary == "Proposed risk log entry for PM-014 (item:PM-014)"
