"""Approving a tracker-changes batch writes it to the tracker (PM-13 gate, PM-26 batch, PM-28 surfaces).

The batch comes from P1's channel outcome record. Until now it could only be rejected. Approving it now creates the new items and adds the
comments through the tracker adapter, behind the same gate as every other write: approvers only, every attempt logged, the proposal marked
applied.

What these tests pin down:

  what is written   a comment on each item the channel line names; a new item for a blocker naming none: blocked, NO assignee, the sprint that
                    covers the day, dated and traced to the channel message; everything tagged `ai-created` so an agent-made entry is never
                    mistaken for a person's, and a first comment on a new item saying where it came from
  idempotent        re-approving, a retry after a failed write, a reworded line, a comment already there: never written twice
  skipped, said     a comment on an item the tracker no longer has, a new item on a day no sprint covers: skipped AND recorded; a batch with
                    nothing writable is refused before approval and stays pending
  the gate          a stranger writes nothing; an edited approval is refused; auto-approve never takes it; a rejection writes nothing
  every surface     a card pressed in Teams, the command line and the dashboard leave the same rows
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from spine.approval.proposals import APPLIED, APPROVED, PENDING, REJECTED, ProposalStore
from streamlit.testing.v1 import AppTest

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.tracker import TrackerItem, TrackerMock
from pm.api import copilot_studio_api as api
from pm.approval import cards, service
from pm.approval import tracker_apply as ta
from pm.approval.audit import TRACKER_WRITE_TYPES as AUDIT_TRACKER_WRITE_TYPES
from pm.approval.audit import audit_trail, describe
from pm.channel.batches import TrackerView, consume

APPROVERS = service.ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
CHANNEL = "19:proj-gamma@thread.tacv2"


def record(*, date="2026-09-18", updates=(), blockers=()) -> dict:
    lines = lambda pairs: [{"message_id": m, "text": t, "quote": None} for m, t in pairs]
    return {
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": date, "allowlisted": True,
        "roster": [], "generated_at": f"{date}T11:30:00+00:00", "participation": [], "decisions": [], "questions": [],
        "updates": lines(updates), "blockers": lines(blockers),
    }


# one comment on an existing item (PM-016) and one new item (the blocker that names none)
DEFAULT = record(updates=[("m1", "PM-016 export bug is fixed and in review.")], blockers=[("m6", "The adapter payload shape is undocumented.")])


def propose(db, tmp_path, data=None) -> str:
    path = tmp_path / f"record_{abs(hash(json.dumps(data or DEFAULT)))}.json"
    path.write_text(json.dumps(data or DEFAULT), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=db), RiskLogMock(db_path=db))
    return consume(path, view, db_path=db).tracker.proposal_id


@pytest.fixture()
def batch(seeded_db_path, tmp_path) -> str:
    return propose(seeded_db_path, tmp_path)


def approve(db, proposal_id, *, user="sharon.silva", **kw):
    return service.approve_and_send(proposal_id, approver_id=user, policy=APPROVERS, db_path=db, **kw)


def tracker_state(db) -> tuple:
    conn = sqlite3.connect(db)
    try:
        return (conn.execute("SELECT * FROM items ORDER BY id").fetchall(), conn.execute("SELECT * FROM item_comments ORDER BY id").fetchall())
    finally:
        conn.close()


def applied(db, proposal_id) -> dict:
    return next(e for e in audit_trail(proposal_id, db_path=db).events if e["action"] == "proposal.applied")["details"]


# --- what is written -------------------------------------------------------------------------------------------------------------


def test_approving_creates_the_new_item_blocked_unassigned_dated_and_traced_to_its_message(seeded_db_path, batch):
    before = {i.id for i in TrackerMock(db_path=seeded_db_path).list_items()}

    result = approve(seeded_db_path, batch)

    (new,) = [i for i in TrackerMock(db_path=seeded_db_path).list_items() if i.id not in before]
    assert result.outcome == service.APPLIED_OUTCOME and new.id == "PM-031"  # the next id after the highest in the tracker
    assert new.title == "The adapter payload shape is undocumented." and new.status == "blocked"
    assert new.assignee_id is None  # nobody is guessed
    assert new.created_at == "2026-09-18" and new.blocked_since == "2026-09-18" and new.source_message_id == "m6"
    assert new.sprint_id == "sprint-13"  # the sprint that covers the day


def test_everything_the_agent_writes_is_tagged_and_a_new_item_says_where_it_came_from(seeded_db_path, batch):
    approve(seeded_db_path, batch)

    tracker = TrackerMock(db_path=seeded_db_path)
    (note,) = tracker.list_comments("PM-031")
    assert note.body == "Created by the PM agent from Project Gamma message m6 on 2026-09-18: The adapter payload shape is undocumented."
    assert set(note.tags) == {ta.FROM_CHANNEL, ta.AI_CREATED}
    comment = next(c for c in tracker.list_comments("PM-016") if "export bug is fixed" in c.body)
    assert comment.body == "From Project Gamma (2026-09-18): PM-016 export bug is fixed and in review."
    assert {ta.FROM_CHANNEL, ta.AI_CREATED, "update"} <= set(comment.tags)  # tagged where it came from, and as made by the agent


def test_the_audit_says_what_was_created_and_commented_and_the_write_is_logged_like_any_other(seeded_db_path, batch):
    approve(seeded_db_path, batch)

    trail = audit_trail(batch, db_path=seeded_db_path)
    assert [e["action"] for e in trail.events] == ["proposal.created", "proposal.approved", "proposal.applied"]
    assert applied(seeded_db_path, batch) == {"target": "tracker", "created": ["PM-031"], "commented": ["PM-016"], "skipped": []}
    assert [(a["action_type"], a["target"], a["status"]) for a in trail.send_attempts] == [("tracker_write", "PM-016,PM-031", "sent")]
    assert trail.status == APPLIED and trail.approver_id == "sharon.silva" and trail.edited is False
    text = describe(trail)
    assert "Approved by sharon.silva" in text and "Written to the tracker" in text and "created PM-031" in text and "commented on PM-016" in text


def test_the_new_blocked_item_is_then_a_real_blocker_the_next_morning_sees(seeded_db_path, batch):
    from pm.state.snapshot import build_current_snapshot

    approve(seeded_db_path, batch)

    snapshot = build_current_snapshot(seeded_db_path, taken_at="2026-09-18T12:00:00+00:00", tz_name="Asia/Colombo")
    assert "PM-031" in {i.id for i in snapshot.items if i.status == "blocked"}


def test_a_batch_that_creates_several_items_gives_each_the_next_id(seeded_db_path, tmp_path):
    data = record(blockers=[("m5", "The vendor feed has no documented schema."), ("m6", "Nobody owns the staging mirror credentials.")])

    approve(seeded_db_path, propose(seeded_db_path, tmp_path, data))

    assert [i.id for i in TrackerMock(db_path=seeded_db_path).list_items()][-2:] == ["PM-031", "PM-032"]


# --- idempotent ------------------------------------------------------------------------------------------------------------------


def test_the_same_batch_is_never_written_twice(seeded_db_path, batch):
    approve(seeded_db_path, batch)
    state = tracker_state(seeded_db_path)

    again = approve(seeded_db_path, batch)
    retry = service.send_approved(batch, policy=APPROVERS, db_path=seeded_db_path)

    assert again.outcome == retry.outcome == service.REFUSED and tracker_state(seeded_db_path) == state


def test_a_later_batch_about_a_message_already_turned_into_an_item_does_not_create_it_again(seeded_db_path, tmp_path):
    approve(seeded_db_path, propose(seeded_db_path, tmp_path))
    items_before = len(TrackerMock(db_path=seeded_db_path).list_items())
    reworded = record(blockers=[("m6", "The adapter payload shape is still undocumented after a second ask.")])  # same message, a new wording
    second = propose(seeded_db_path, tmp_path, reworded)
    assert ProposalStore(seeded_db_path).get(second).status == PENDING

    result = approve(seeded_db_path, second)

    assert result.outcome == service.REFUSED and "nothing to write" in result.detail and "already created from this message" in result.detail
    assert len(TrackerMock(db_path=seeded_db_path).list_items()) == items_before
    assert len(TrackerMock(db_path=seeded_db_path).list_comments("PM-031")) == 1  # a reworded line adds no second "created by" note
    assert ProposalStore(seeded_db_path).get(second).status == PENDING  # refused before approval, so it can still be rejected


def test_a_comment_the_item_already_has_is_skipped_not_repeated_and_the_audit_says_so(seeded_db_path, tmp_path):
    data = record(updates=[("m1", "PM-016 export bug is fixed and in review."), ("m2", "PM-009 tests are written.")])
    TrackerMock(db_path=seeded_db_path).add_comment("PM-016", "From Project Gamma (2026-09-18): PM-016 export bug is fixed and in review.", [])
    batch = propose(seeded_db_path, tmp_path, data)

    result = approve(seeded_db_path, batch)

    done = applied(seeded_db_path, batch)
    assert result.outcome == service.APPLIED_OUTCOME and done["commented"] == ["PM-009"]
    assert done["skipped"][0]["reason"] == "PM-016 already has this comment" and done["skipped"][0]["message_id"] == "m1"
    assert "1 skipped" in result.detail


# --- skipped, and said -------------------------------------------------------------------------------------------------------------


def test_a_comment_on_an_item_the_tracker_no_longer_has_is_skipped_and_recorded(seeded_db_path, tmp_path):
    data = record(updates=[("m1", "PM-016 export bug is fixed."), ("m2", "PM-009 tests are written.")])
    batch = propose(seeded_db_path, tmp_path, data)
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM item_comments WHERE item_id = 'PM-016'")
    conn.execute("DELETE FROM item_transitions WHERE item_id = 'PM-016'")
    conn.execute("DELETE FROM items WHERE id = 'PM-016'")  # the lead removed it after the batch was proposed
    conn.commit()
    conn.close()

    result = approve(seeded_db_path, batch)

    done = applied(seeded_db_path, batch)
    assert result.outcome == service.APPLIED_OUTCOME and done["commented"] == ["PM-009"]
    assert done["skipped"] == [{"channel": "Project Gamma", "message_id": "m1", "kind": "comment", "item_id": "PM-016",
                                "reason": "PM-016 is no longer in the tracker"}]


def test_a_new_item_on_a_day_no_sprint_covers_is_skipped_because_an_item_needs_a_sprint(seeded_db_path, tmp_path):
    data = record(date="2025-01-01", updates=[("m1", "PM-016 export bug is fixed.")], blockers=[("m6", "The adapter payload shape is undocumented.")])
    batch = propose(seeded_db_path, tmp_path, data)

    result = approve(seeded_db_path, batch)

    done = applied(seeded_db_path, batch)
    assert result.outcome == service.APPLIED_OUTCOME and done["created"] == [] and done["commented"] == ["PM-016"]
    assert done["skipped"][0]["reason"].startswith("no sprint covers 2025-01-01, and an item needs a sprint") and "PM_TRACKER_DEFAULT_SPRINT" in done["skipped"][0]["reason"]


def test_a_configured_default_sprint_takes_the_items_no_sprint_covers_and_the_audit_says_so(seeded_db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_TRACKER_DEFAULT_SPRINT", "sprint-13")
    data = record(date="2026-10-07", blockers=[("m6", "The adapter payload shape is undocumented.")])  # a day no seeded sprint covers
    batch = propose(seeded_db_path, tmp_path, data)

    result = approve(seeded_db_path, batch)

    (new,) = [i for i in TrackerMock(db_path=seeded_db_path).list_items() if i.source_message_id == "m6"]
    assert result.outcome == service.APPLIED_OUTCOME and new.sprint_id == "sprint-13" and new.created_at == "2026-10-07"
    assert "went to sprint-13, the configured default" in result.detail and applied(seeded_db_path, batch)["default_sprint"] == "sprint-13"


def test_a_sprint_a_day_does_cover_is_never_replaced_by_the_default(seeded_db_path, batch, monkeypatch):
    monkeypatch.setenv("PM_TRACKER_DEFAULT_SPRINT", "sprint-12")

    approve(seeded_db_path, batch)

    (new,) = [i for i in TrackerMock(db_path=seeded_db_path).list_items() if i.source_message_id == "m6"]
    assert new.sprint_id == "sprint-13" and "default_sprint" not in applied(seeded_db_path, batch)


def test_a_default_sprint_the_tracker_does_not_have_is_not_used_and_is_said(seeded_db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_TRACKER_DEFAULT_SPRINT", "sprint-99")
    batch = propose(seeded_db_path, tmp_path, record(date="2026-10-07", blockers=[("m6", "The adapter payload shape is undocumented.")]))

    result = approve(seeded_db_path, batch)

    assert result.outcome == service.REFUSED and "'sprint-99' is not in the tracker" in result.detail
    assert ProposalStore(seeded_db_path).get(batch).status == PENDING


def test_a_batch_with_nothing_writable_is_refused_before_approval_and_stays_pending(seeded_db_path, tmp_path):
    batch = propose(seeded_db_path, tmp_path, record(date="2025-01-01", blockers=[("m6", "The adapter payload shape is undocumented.")]))
    state = tracker_state(seeded_db_path)

    result = approve(seeded_db_path, batch)

    assert result.outcome == service.REFUSED and "nothing to write" in result.detail and "no sprint covers 2025-01-01" in result.detail
    assert ProposalStore(seeded_db_path).get(batch).status == PENDING and tracker_state(seeded_db_path) == state
    assert audit_trail(batch, db_path=seeded_db_path).events[-1]["action"] == "proposal.denied"


# --- a write that fails part-way can be retried, and lands once --------------------------------------------------------------------


class FlakyTracker(TrackerMock):
    """Fails after the new item exists but before its first comment: the worst place."""

    def __init__(self, db_path, failures: int) -> None:
        super().__init__(db_path)
        self.failures = failures

    def add_comment(self, item_id, body, tags=None):
        if self.failures and item_id == "PM-031":
            self.failures -= 1
            raise OSError("tracker went away")
        return super().add_comment(item_id, body, tags)


def test_a_write_that_fails_halfway_leaves_the_proposal_approved_and_a_retry_finishes_it_without_repeating_anything(seeded_db_path, tmp_path):
    clean_db = tmp_path / "clean.db"
    shutil.copy(seeded_db_path, clean_db)
    approve(clean_db, propose(clean_db, tmp_path))  # a clean run, for comparison
    batch = propose(seeded_db_path, tmp_path)
    flaky = FlakyTracker(seeded_db_path, failures=1)

    first = approve(seeded_db_path, batch, tracker=flaky)

    assert first.outcome == service.SEND_FAILED and "tracker went away" in first.detail
    assert ProposalStore(seeded_db_path).get(batch).status == APPROVED
    assert [i.id for i in TrackerMock(db_path=seeded_db_path).list_items()][-1] == "PM-031"  # the item landed, its note did not

    retry = service.send_approved(batch, policy=APPROVERS, tracker=flaky, db_path=seeded_db_path)

    assert retry.outcome == service.APPLIED_OUTCOME
    tracker = TrackerMock(db_path=seeded_db_path)
    assert len([i for i in tracker.list_items() if i.source_message_id == "m6"]) == 1  # one item, not two
    assert len(tracker.list_comments("PM-031")) == 1 and len(tracker.list_comments("PM-016")) == len(TrackerMock(db_path=clean_db).list_comments("PM-016"))


def test_a_tracker_write_and_its_retry_need_no_teams_publisher(seeded_db_path, batch, monkeypatch):
    """Writing to the tracker posts nothing, so a Teams publisher that is missing or misconfigured must not stop it."""
    def no_publisher():
        raise RuntimeError("TEAMS_PUBLISHER_MODE=power_automate but POWER_AUTOMATE_FLOW_URL is not set")

    monkeypatch.setattr(service, "get_teams_publisher", no_publisher)
    flaky = FlakyTracker(seeded_db_path, failures=1)

    first = approve(seeded_db_path, batch, tracker=flaky)
    retry = service.send_approved(batch, policy=APPROVERS, tracker=flaky, db_path=seeded_db_path)

    assert first.outcome == service.SEND_FAILED and retry.outcome == service.APPLIED_OUTCOME  # neither asked for a publisher


# --- the gate ---------------------------------------------------------------------------------------------------------------------


def test_a_stranger_writes_nothing_to_the_tracker(seeded_db_path, batch):
    state = tracker_state(seeded_db_path)

    result = approve(seeded_db_path, batch, user="mallory")

    assert result.outcome == service.REFUSED and tracker_state(seeded_db_path) == state
    assert ProposalStore(seeded_db_path).get(batch).status == PENDING and audit_trail(batch, db_path=seeded_db_path).events[-1]["action"] == "proposal.denied"


def test_a_batch_is_approved_whole_an_edited_approval_is_refused(seeded_db_path, batch):
    state = tracker_state(seeded_db_path)

    result = approve(seeded_db_path, batch, edited_content="create twenty items instead")

    assert result.outcome == service.REFUSED and "cannot be edited" in result.detail and tracker_state(seeded_db_path) == state
    assert ProposalStore(seeded_db_path).get(batch).status == PENDING


@pytest.mark.parametrize("nothing", ["", "   ", "\n"])
def test_a_blank_edit_from_a_flow_with_no_edit_box_is_not_a_refusal(seeded_db_path, batch, nothing):
    assert approve(seeded_db_path, batch, edited_content=nothing).outcome == service.APPLIED_OUTCOME


def test_a_severity_means_nothing_for_a_tracker_batch_and_is_ignored(seeded_db_path, batch):
    assert approve(seeded_db_path, batch, severity="high").outcome == service.APPLIED_OUTCOME


def test_auto_approve_never_takes_a_tracker_batch(seeded_db_path, batch):
    result = service.auto_approve_and_send(batch, policy=service.ApprovalPolicy(approver_ids=APPROVERS.approver_ids, auto_approve=True),
                                           db_path=seeded_db_path)

    assert result.outcome == service.HELD and "never auto-approved" in result.detail and ProposalStore(seeded_db_path).get(batch).status == PENDING


def test_rejecting_a_tracker_batch_writes_nothing(seeded_db_path, batch):
    state = tracker_state(seeded_db_path)

    result = service.reject(batch, approver_id="sharon.silva", reason="not real blockers", policy=APPROVERS, db_path=seeded_db_path)

    assert result.outcome == service.REJECTED_OUTCOME and tracker_state(seeded_db_path) == state
    assert ProposalStore(seeded_db_path).get(batch).status == REJECTED


def test_a_proposal_nobody_approved_is_never_written(seeded_db_path, batch):
    state = tracker_state(seeded_db_path)

    assert service.send_approved(batch, policy=APPROVERS, db_path=seeded_db_path).outcome == service.REFUSED
    assert service._execute(batch, actor="agent", publisher=None, policy=APPROVERS, store=ProposalStore(seeded_db_path),
                            db_path=seeded_db_path).outcome == service.REFUSED
    assert tracker_state(seeded_db_path) == state


def test_the_audit_module_and_the_applier_agree_on_which_types_write_to_the_tracker():
    assert AUDIT_TRACKER_WRITE_TYPES == ta.TRACKER_WRITE_TYPES == service.TRACKER_WRITE_TYPES


def test_the_decision_card_says_the_tracker_was_written_not_that_a_brief_was_sent(seeded_db_path, batch):
    approve(seeded_db_path, batch)

    text = json.dumps(cards.decision_card(audit_trail(batch, db_path=seeded_db_path)))

    assert "Tracker changes approved" in text and "Written to the tracker" in text and "PM-016,PM-031" in text and "Morning brief" not in text


def test_the_tracker_reads_comments_in_order_and_refuses_an_unknown_item(seeded_db_path):
    tracker = TrackerMock(db_path=seeded_db_path)
    tracker.add_comment("PM-016", "first", ["a"])
    tracker.add_comment("PM-016", "second", [])

    assert [c.body for c in tracker.list_comments("PM-016")][-2:] == ["first", "second"]
    assert tracker.list_comments("PM-016")[-2].tags == ["a"]
    with pytest.raises(Exception, match="PM-999"):
        tracker.list_comments("PM-999")


def test_the_planner_alone_changes_nothing(seeded_db_path, batch):
    state = tracker_state(seeded_db_path)

    plan = ta.plan_writes(ProposalStore(seeded_db_path).get(batch), tracker=TrackerMock(db_path=seeded_db_path))

    assert plan.created == ["PM-031"] and plan.commented == ["PM-016"] and tracker_state(seeded_db_path) == state
    assert isinstance(next(op for op in plan.ops if op.kind == "create").item, TrackerItem)


# --- every surface leaves the same rows ------------------------------------------------------------------------------------------


def _record(db, proposal_id) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        audit = [dict(r) for r in conn.execute("SELECT actor, action, details FROM audit WHERE entity_id = ? ORDER BY id", (proposal_id,))]
        writes = [dict(r) for r in conn.execute("SELECT action_type, target, status FROM write_log WHERE proposal_id = ? ORDER BY id", (proposal_id,))]
        proposal = dict(conn.execute("SELECT type, status, approver_id, payload FROM proposals WHERE id = ?", (proposal_id,)).fetchone())
        items = [dict(r) for r in conn.execute("SELECT * FROM items WHERE id > 'PM-030' ORDER BY id")]
        comments = [{k: v for k, v in dict(r).items() if k not in ("id", "created_at")}
                    for r in conn.execute("SELECT * FROM item_comments WHERE item_id = 'PM-031' OR body LIKE 'From Project Gamma%' ORDER BY id")]
    finally:
        conn.close()
    return {"audit": audit, "write_log": writes, "proposal": proposal, "items": items, "comments": comments}


def test_a_card_pressed_in_teams_writes_the_same_rows_as_the_command_line(seeded_db_path, batch, tmp_path, monkeypatch):
    other = tmp_path / "fallback.db"
    shutil.copy(seeded_db_path, other)
    service.approve_and_send(batch, approver_id="sharon.silva", policy=APPROVERS, db_path=other)
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_COPILOT_API_KEY", "k")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")

    with TestClient(api.app) as client:
        result = client.post("/card_action", json={"action": "approve", "proposal_id": batch},
                             headers={"X-API-Key": "k", "X-Authenticated-User": "sharon.silva"}).json()

    assert result["outcome"] == service.APPLIED_OUTCOME
    assert _record(seeded_db_path, batch) == _record(other, batch)


def test_a_stranger_pressing_the_card_writes_nothing(seeded_db_path, batch, monkeypatch):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_COPILOT_API_KEY", "k")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    state = tracker_state(seeded_db_path)

    with TestClient(api.app) as client:
        result = client.post("/card_action", json={"action": "approve", "proposal_id": batch},
                             headers={"X-API-Key": "k", "X-Authenticated-User": "mallory@example.com"}).json()

    assert result["outcome"] == service.REFUSED and tracker_state(seeded_db_path) == state


def test_the_command_line_approves_a_tracker_batch(seeded_db_path, batch, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("approve_cli", Path(__file__).resolve().parents[2] / "scripts" / "approve.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")

    code = cli.main(["--db", str(seeded_db_path), "approve", batch, "--as", "sharon.silva"])

    assert code == 0 and "applied: in the tracker: created PM-031 and added 1 comment(s) on PM-016" in capsys.readouterr().out


def test_the_dashboard_approves_a_tracker_batch_and_offers_no_edit_box_or_severity(seeded_db_path, batch, monkeypatch, tmp_path):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("P1_DB_PATH", str(tmp_path / "no_p1.db"))
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")
    monkeypatch.setenv("PM_DASHBOARD_USER", "")
    page = str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py")
    at = AppTest.from_file(page, default_timeout=30).run()
    at.selectbox(key="acting_as").select("sharon.silva").run()
    assert not at.exception, [e.value for e in at.exception]
    assert f"approve_{batch}" in {b.key for b in at.button} and f"severity_{batch}" not in {s.key for s in at.selectbox}
    assert f"edit_{batch}" not in {t.key for t in at.text_area}

    next(b for b in at.button if b.key == f"approve_{batch}").click().run()

    assert not at.exception, [e.value for e in at.exception]
    assert ProposalStore(seeded_db_path).get(batch).status == APPLIED and "PM-031" in {i.id for i in TrackerMock(db_path=seeded_db_path).list_items()}
