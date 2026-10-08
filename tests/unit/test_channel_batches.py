"""PM-26: P1's outcome record becomes two batched proposal sets, tracker updates or creations and risk-log entries,
each item carrying the channel and the message that justify it.

The rules are plain Python, no model (the record's lines are already grounded by P1):

  tracker batch   a line naming a tracker item that exists  -> a comment on that item, tagged with where it came from
                  a blocker naming no item at all           -> a new item to track it (status blocked, no assignee, never guessed)
  risk batch      a blocker naming an item with no open risk-log entry -> a risk entry for it (owner only if the tracker has one)
                  a blocker naming no item                             -> a risk entry with no item
  neither         a line naming an item the tracker does not have, or an update/decision/question naming none: skipped, with the reason

Consuming a record writes nothing to the tracker or the risk log: the two batches are proposals, approved by a person like every other.
Approving the risk batch writes its entries to the risk log (tests/unit/test_risk_approval.py); approving the tracker batch writes the
items and comments to the tracker (tests/unit/test_tracker_approval.py).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from spine.approval.proposals import PENDING, REJECTED, ProposalStore

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.tracker import TrackerMock
from pm.approval import service
from pm.channel.batches import (
    CHANNEL_RISK_PROPOSAL_TYPE,
    CHANNEL_TRACKER_PROPOSAL_TYPE,
    TrackerView,
    consume,
    plan_batches,
)
from pm.channel.record import load_record

CHANNEL = "19:proj-gamma@thread.tacv2"


def record_dict(**overrides) -> dict:
    data = {
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": "2026-09-18",
        "allowlisted": True, "roster": ["wei.chen"], "generated_at": "2026-09-18T11:30:00+00:00",
        "updates": [
            {"message_id": "m1", "text": "PM-016 export bug is fixed and in review.", "quote": "export bug is fixed"},
            {"message_id": "m2", "text": "Wrote the new retry tests.", "quote": None},  # names no item
        ],
        "decisions": [{"message_id": "m3", "text": "We will keep the five minute poll.", "quote": None}],  # names no item
        "blockers": [
            {"message_id": "m4", "text": "PM-014 is still blocked on the staging migration.", "quote": None},  # blocked, no risk entry
            {"message_id": "m5", "text": "PM-023 is still blocked, the vendor has not replied.", "quote": None},  # blocked, has RISK-001
            {"message_id": "m6", "text": "The adapter payload shape is undocumented.", "quote": None},  # names no item
            {"message_id": "m7", "text": "PM-999 is blocked on a vendor.", "quote": None},  # an item the tracker does not have
        ],
        "questions": [{"message_id": "m8", "text": "Does PM-016 need a design review?", "quote": None}],
        "participation": [],
    }
    data.update(overrides)
    return data


@pytest.fixture()
def world(seeded_db_path, tmp_path):
    class World:
        db = seeded_db_path
        tmp = tmp_path
        view = TrackerView.from_adapters(TrackerMock(db_path=seeded_db_path), RiskLogMock(db_path=seeded_db_path))

        @staticmethod
        def file(data=None, name="2026-09-18.json") -> Path:
            path = tmp_path / name
            path.write_text(json.dumps(data if data is not None else record_dict()), encoding="utf-8")
            return path

        @staticmethod
        def consume(data=None, **kw):
            return consume(World.file(data), World.view, db_path=World.db, **kw)

    return World


def _plan(world, data=None):
    return plan_batches(load_record(world.file(data)), world.view)


# --- the plan ---------------------------------------------------------------------------------------------------------------


def test_a_line_naming_an_existing_item_becomes_a_comment_on_it(world):
    plan = _plan(world)

    comments = [i for i in plan.tracker_items if i["kind"] == "comment"]
    assert sorted((c["item_id"], c["reference"]["message_id"], c["reference"]["section"]) for c in comments) == [
        ("PM-014", "m4", "blocker"), ("PM-016", "m1", "update"), ("PM-016", "m8", "question"), ("PM-023", "m5", "blocker"),
    ]
    first = next(c for c in comments if c["reference"]["message_id"] == "m1")
    assert "PM-016 export bug is fixed and in review." in first["body"] and "Project Gamma" in first["body"] and "2026-09-18" in first["body"]
    assert first["tags"] == ["from-channel", "update"]


def test_a_blocker_naming_no_item_becomes_a_new_item_with_nobody_assigned(world):
    plan = _plan(world)

    (created,) = [i for i in plan.tracker_items if i["kind"] == "create"]
    assert created["reference"]["message_id"] == "m6" and created["status"] == "blocked"
    assert created["title"].startswith("The adapter payload shape is undocumented")
    assert created["assignee_id"] is None  # never guessed
    assert created["sprint_id"] == "sprint-13"  # the sprint that covers the day, from the tracker


def test_a_blocker_on_an_item_with_no_open_risk_entry_becomes_a_risk_entry_with_the_trackers_owner(world):
    plan = _plan(world)

    entries = {(i["related_item_id"], i["reference"]["message_id"]): i for i in plan.risk_items}
    assert set(entries) == {("PM-014", "m4"), (None, "m6")}  # PM-023 already has RISK-001; PM-999 is unknown
    assert entries[("PM-014", "m4")]["suggested_owner"] == "olivia.dupree"  # PM-014's assignee in the tracker
    assert entries[(None, "m6")]["suggested_owner"] is None
    assert "severity" not in entries[("PM-014", "m4")]  # not guessed: the lead sets it


def test_what_is_skipped_is_listed_with_the_reason(world):
    plan = _plan(world)

    reasons = {(s["set"], s["message_id"]): s["reason"] for s in plan.skipped}
    assert "PM-999" in reasons[("tracker", "m7")] and "not in the tracker" in reasons[("tracker", "m7")]
    assert "PM-999" in reasons[("risk", "m7")]
    assert "RISK-001" in reasons[("risk", "m5")] and "already" in reasons[("risk", "m5")]
    assert "names no tracker item" in reasons[("tracker", "m2")] and ("tracker", "m3") in reasons


def test_every_item_in_both_batches_carries_the_channel_and_message_that_justify_it(world):
    plan = _plan(world)

    items = [*plan.tracker_items, *plan.risk_items]
    assert items
    for item in items:
        ref = item["reference"]
        assert ref["channel_id"] == CHANNEL and ref["channel_display_name"] == "Project Gamma" and ref["date"] == "2026-09-18"
        assert ref["message_id"] and ref["ref"] == f"teams:{CHANNEL}:{ref['message_id']}"
        assert item["fingerprint"]


def test_the_same_record_always_plans_the_same_items(world):
    assert _plan(world) == _plan(world)


def test_a_line_naming_two_items_comments_on_each(world):
    data = record_dict(updates=[{"message_id": "m1", "text": "PM-016 and PM-009 were both reviewed today.", "quote": None}])

    plan = _plan(world, data)

    assert sorted(i["item_id"] for i in plan.tracker_items if i["kind"] == "comment" and i["reference"]["message_id"] == "m1") == ["PM-009", "PM-016"]


def test_a_day_no_sprint_covers_still_plans_the_item_with_no_sprint(world):
    plan = _plan(world, record_dict(date="2025-01-01"))

    (created,) = [i for i in plan.tracker_items if i["kind"] == "create"]
    assert created["sprint_id"] is None


def test_several_lines_from_one_message_are_one_item_not_one_each(world):
    """P1 splits a long message into several grounded lines; the reference is the message, so the batch proposes it once."""
    data = record_dict(
        updates=[], decisions=[], questions=[],
        blockers=[
            {"message_id": "m6", "text": "The adapter payload shape is undocumented.", "quote": "payload shape"},
            {"message_id": "m6", "text": "The adapter work is parked until it is provided.", "quote": "parked"},
            {"message_id": "m4", "text": "PM-014 is blocked on the staging migration.", "quote": None},
            {"message_id": "m4", "text": "Nobody has said when the migration will run.", "quote": None},
        ],
    )

    plan = _plan(world, data)

    (created,) = [i for i in plan.tracker_items if i["kind"] == "create"]
    assert created["reference"]["message_id"] == "m6" and created["reference"]["quote"] == "payload shape"
    assert created["source_text"] == "The adapter payload shape is undocumented. The adapter work is parked until it is provided."
    (comment,) = [i for i in plan.tracker_items if i["kind"] == "comment"]
    assert comment["item_id"] == "PM-014" and "Nobody has said when the migration will run." in comment["body"]
    assert sorted(i["reference"]["message_id"] for i in plan.risk_items) == ["m4", "m6"]


def test_one_message_that_is_both_an_update_and_a_blocker_is_two_items(world):
    data = record_dict(
        updates=[{"message_id": "m1", "text": "PM-016 export bug is fixed.", "quote": None}], decisions=[], questions=[],
        blockers=[{"message_id": "m1", "text": "PM-016 still needs a design review.", "quote": None}],
    )

    plan = _plan(world, data)

    assert sorted(i["reference"]["section"] for i in plan.tracker_items) == ["blocker", "update"]


# --- the two proposals ------------------------------------------------------------------------------------------------------


def test_consuming_a_record_proposes_two_batches_and_writes_nothing_else(world):
    before = _counts(world.db)

    result = world.consume()

    assert result.refused is None
    tracker, risk = ProposalStore(world.db).get(result.tracker.proposal_id), ProposalStore(world.db).get(result.risk.proposal_id)
    assert (tracker.type, tracker.status) == (CHANNEL_TRACKER_PROPOSAL_TYPE, PENDING)
    assert (risk.type, risk.status) == (CHANNEL_RISK_PROPOSAL_TYPE, PENDING)
    assert len(tracker.payload["items"]) == 5 and len(risk.payload["items"]) == 2
    assert tracker.payload["channel_id"] == CHANNEL and tracker.payload["date"] == "2026-09-18" and tracker.payload["record_schema_version"] == "1.0"
    assert f"teams:{CHANNEL}:m4" in tracker.source_refs and f"teams:{CHANNEL}:m6" in risk.source_refs
    assert tracker.original_model_output["items"] == tracker.payload["items"]  # what was proposed, kept as it was
    after = _counts(world.db)
    assert {k: after[k] - before[k] for k in after} == {"items": 0, "comments": 0, "risks": 0, "proposals": 2, "commitments": 0}


def test_approving_the_tracker_batch_writes_to_the_tracker_and_nowhere_else(world):
    result = world.consume()
    assert CHANNEL_TRACKER_PROPOSAL_TYPE in service.EXECUTABLE_TYPES and CHANNEL_TRACKER_PROPOSAL_TYPE not in service.MESSAGE_TYPES
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
    before = _counts(world.db)

    approved = service.approve_and_send(result.tracker.proposal_id, approver_id="sharon.silva", publisher=None, policy=policy, db_path=world.db)

    after = _counts(world.db)
    assert approved.outcome == service.APPLIED_OUTCOME and ProposalStore(world.db).get(result.tracker.proposal_id).status == "applied"
    # one new item (the blocker that names none), and its four comments plus the note saying where the item came from; the risk log is untouched
    assert {k: after[k] - before[k] for k in after} == {"items": 1, "comments": 5, "risks": 0, "proposals": 0, "commitments": 0}


def test_approving_the_risk_batch_is_the_one_step_that_writes_to_the_risk_log(world):
    result = world.consume()
    assert CHANNEL_RISK_PROPOSAL_TYPE in service.EXECUTABLE_TYPES
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
    before = _counts(world.db)

    approved = service.approve_and_send(result.risk.proposal_id, approver_id="sharon.silva", severity="high", publisher=None, policy=policy,
                                        db_path=world.db)

    after = _counts(world.db)
    assert approved.outcome == service.APPLIED_OUTCOME
    assert {k: after[k] - before[k] for k in after} == {"items": 0, "comments": 0, "risks": 2, "proposals": 0, "commitments": 0}


def test_reading_the_same_record_again_proposes_nothing_new(world):
    first = world.consume()

    again = world.consume()

    assert again.tracker.created is False and again.risk.created is False
    assert (again.tracker.proposal_id, again.risk.proposal_id) == (first.tracker.proposal_id, first.risk.proposal_id)
    assert _counts(world.db)["proposals"] == 2


def test_a_rejected_item_is_not_proposed_again(world):
    first = world.consume()
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
    service.reject(first.risk.proposal_id, approver_id="sharon.silva", reason="not a risk", policy=policy, db_path=world.db)
    assert ProposalStore(world.db).get(first.risk.proposal_id).status == REJECTED

    again = world.consume()

    assert again.risk.proposal_id is None and again.risk.created is False and again.risk.remembered == 2
    assert _counts(world.db)["proposals"] == 2


def test_a_regenerated_record_proposes_only_what_is_new(world):
    world.consume()
    data = record_dict()
    data["blockers"].append({"message_id": "m9", "text": "PM-015 is blocked waiting for the vendor.", "quote": None})

    again = world.consume(data)

    new_tracker = ProposalStore(world.db).get(again.tracker.proposal_id)
    assert again.tracker.created and [i["reference"]["message_id"] for i in new_tracker.payload["items"]] == ["m9"]
    assert [i["reference"]["message_id"] for i in ProposalStore(world.db).get(again.risk.proposal_id).payload["items"]] == ["m9"]
    assert again.tracker.remembered == 5


def test_a_line_whose_wording_changed_is_proposed_again_and_says_what_it_replaces(world):
    first = world.consume()
    data = record_dict()
    data["blockers"][2]["text"] = "The adapter payload shape is still undocumented after a second request."

    again = world.consume(data)

    item = ProposalStore(world.db).get(again.risk.proposal_id).payload["items"][0]
    assert again.risk.created and item["reference"]["message_id"] == "m6"
    assert item["changed_since"]["earlier_proposal_id"] == first.risk.proposal_id
    assert item["changed_since"]["earlier_text"] == "The adapter payload shape is undocumented."


def test_a_dry_run_plans_and_proposes_nothing(world):
    before = _counts(world.db)

    result = world.consume(dry_run=True)

    assert result.tracker.proposal_id is None and result.tracker.items == 5 and result.risk.items == 2
    assert _counts(world.db) == before


def test_a_record_that_is_not_cleared_gives_zero_proposals_and_one_logged_refusal(world):
    before = _counts(world.db)

    result = world.consume(record_dict(allowlisted=False))

    assert result.refused.code == "not_allowlisted" and result.tracker is None and result.risk is None
    assert _counts(world.db) == before
    conn = sqlite3.connect(world.db)
    rows = conn.execute("SELECT action, entity_type, details FROM audit WHERE entity_type = 'outcome_record'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "record.refused" and json.loads(rows[0][2])["code"] == "not_allowlisted"


def test_a_record_with_nothing_to_propose_creates_no_empty_batch(world):
    quiet = record_dict(updates=[], decisions=[], blockers=[], questions=[])

    result = world.consume(quiet)

    assert result.tracker.proposal_id is None and result.tracker.items == 0 and result.risk.items == 0
    assert _counts(world.db)["proposals"] == 0


def _counts(db) -> dict:
    conn = sqlite3.connect(db)
    return {
        "items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "comments": conn.execute("SELECT COUNT(*) FROM item_comments").fetchone()[0],
        "risks": conn.execute("SELECT COUNT(*) FROM risks").fetchone()[0],
        "proposals": conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0],
        "commitments": conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0],
    }
