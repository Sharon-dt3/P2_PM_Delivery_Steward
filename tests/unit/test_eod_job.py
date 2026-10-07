"""PM-22 on the platform: the end-of-day job builds the summary from the stored diff and proposes it.

The job takes the end-of-day snapshot, finds the day's stored morning snapshot, diffs the two, has the model
word the changes (grounded), and PROPOSES the summary: nothing is posted until the approval gate lets it.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import PENDING, REJECTED, ProposalStore
from streamlit.testing.v1 import AppTest

from pm.approval import service
from pm.approval.cards import pending_brief_card
from pm.approval.proposals import (
    ALREADY_PROPOSED,
    AUTO_SENT,
    EOD_PROPOSAL_TYPE,
    PROPOSED,
)
from pm.approval.service import ApprovalPolicy
from pm.jobs.end_of_day_job import (
    CAPTURED,
    SKIPPED_NON_WORKING_DAY,
    SUMMARISED,
    run_end_of_day_job,
)
from pm.jobs.snapshot_capture import capture_snapshot
from pm.reporting.scripted_summary import ScriptedSummaryGateway
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID
from pm.state.store import list_snapshot_timestamps

MORNING = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # Wed 08:00 in Colombo
EVENING = datetime(2026, 9, 16, 11, 30, tzinfo=timezone.utc)  # Wed 17:00 in Colombo
ID_RE = re.compile(r"\bPM-\d+\b")
CHANGED = {"PM-016", "PM-201", "PM-202", "PM-204"}  # PM-016 is done by 10:00 UTC in the seed; the rest are added below
ONE_FIRST_HUMAN = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), auto_approve=True, auto_approve_requires_first_human=False)
MANUAL = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))


def _config():
    return ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                 morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))


def _add(db, item_id, title, status, history, assignee="aisha.rahman"):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id) "
                 "VALUES (?, ?, ?, 'sprint-13', ?, '2026-09-01', NULL, NULL)", (item_id, title, status, assignee))
    for frm, to, at in history:
        conn.execute("INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?)", (item_id, frm, to, at))
    conn.commit()
    conn.close()


@pytest.fixture()
def db(seeded_db_path):
    """The seeded project plus three changes made between 08:00 and 17:00 on 16 Sep."""
    _add(seeded_db_path, "PM-201", "Ship the export button", "done", [("in_progress", "done", "2026-09-16T06:00:00+00:00")])
    _add(seeded_db_path, "PM-202", "Wire the audit log", "blocked", [("in_progress", "blocked", "2026-09-16T07:00:00+00:00")])
    _add(seeded_db_path, "PM-204", "Review the pricing page", "in_review", [("in_progress", "in_review", "2026-09-16T08:00:00+00:00")])
    return seeded_db_path


def _run(db, *, gateway=None, store_morning=True, policy=None, moment=EVENING, publisher=None):
    if store_morning:
        capture_snapshot(_config(), MORNING, db_path=db)
    return run_end_of_day_job(_config(), gateway or ScriptedSummaryGateway(), moment=moment, db_path=db,
                              policy=policy or MANUAL, publisher=publisher)


def _eod_proposals(db):
    store = ProposalStore(db)
    return [p for s in ("pending", "approved", "rejected", "applied") for p in store.list_by_status(s) if p.type == EOD_PROPOSAL_TYPE]


# --- the job builds the summary from the stored diff ------------------------------------------------------------------------


def test_the_job_proposes_a_summary_that_names_exactly_what_changed(db):
    result = _run(db)

    assert result.status == SUMMARISED and result.morning_source == "stored"
    (proposal,) = _eod_proposals(db)
    content = proposal.payload["content"]
    assert content.startswith("End-of-day summary — 2026-09-16")
    assert set(ID_RE.findall(content)) == CHANGED and len(ID_RE.findall(content)) == len(CHANGED)  # each, once
    assert proposal.status == PENDING and proposal.id == result.proposal_id and result.delivery_status == PROPOSED


def test_the_job_files_each_change_under_the_right_heading(db):
    _run(db)

    content = _eod_proposals(db)[0].payload["content"]
    shipped = content.split("## What shipped")[1].split("## ")[0]
    pending = content.split("## What is still pending")[1].split("## ")[0]
    blocked = content.split("## What is newly blocked")[1].split("## ")[0]
    assert "PM-201" in shipped and "PM-016" in shipped and "PM-204" in pending and "PM-202" in blocked


def test_the_job_uses_the_stored_morning_snapshot_not_a_later_one(db):
    capture_snapshot(_config(), datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc), db_path=db)  # a second, later one that morning

    result = _run(db)

    assert result.summary.facts.morning_taken_at == MORNING.isoformat()  # the earliest of the day, the real morning one
    assert result.morning_source == "stored"


def test_without_a_stored_morning_snapshot_it_is_rebuilt_and_the_summary_says_so(db):
    result = _run(db, store_morning=False)

    assert result.morning_source == "reconstructed"
    (proposal,) = _eod_proposals(db)
    assert set(ID_RE.findall(proposal.payload["content"])) == CHANGED  # the same answer, from the tracker's history
    assert "rebuilt from the tracker's history" in proposal.payload["content"]
    assert list_snapshot_timestamps(db) == [EVENING.isoformat()]  # the rebuilt one was not saved as if it were real


def test_a_snapshot_from_another_day_is_not_the_morning(db):
    capture_snapshot(_config(), datetime(2026, 9, 15, 2, 30, tzinfo=timezone.utc), db_path=db)  # yesterday's morning

    result = _run(db, store_morning=False)

    assert result.morning_source == "reconstructed"


def test_running_it_again_does_not_propose_a_second_summary(db):
    first = _run(db)

    second = run_end_of_day_job(_config(), ScriptedSummaryGateway(), moment=EVENING, db_path=db, policy=MANUAL)

    assert len(_eod_proposals(db)) == 1 and second.delivery_status == ALREADY_PROPOSED and second.proposal_id == first.proposal_id


def test_without_a_gateway_it_only_captures_the_snapshot_as_before(db):
    result = run_end_of_day_job(_config(), moment=EVENING, db_path=db)

    assert result.status == CAPTURED and result.summary is None and _eod_proposals(db) == []


def test_a_non_working_day_is_skipped_before_any_work(db):
    result = run_end_of_day_job(_config(), ScriptedSummaryGateway(), moment=datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc), db_path=db)

    assert result.status == SKIPPED_NON_WORKING_DAY and _eod_proposals(db) == [] and list_snapshot_timestamps(db) == []


def test_a_failure_in_the_summary_never_loses_the_snapshot(db, monkeypatch):
    import pm.jobs.end_of_day_job as job

    monkeypatch.setattr(job, "compute_delta", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("diff fell over")))

    result = _run(db)

    assert result.status == CAPTURED and "summary failed" in result.detail and EVENING.isoformat() in list_snapshot_timestamps(db)


def test_a_model_that_fails_outright_still_gives_the_recorded_changes(db):
    from spine.llm.gateway import LLMResponse

    class Broken:
        def generate(self, prompt, **kwargs):
            return LLMResponse(text="nope", provider="f", model="f", prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False)

    _run(db, gateway=Broken())

    content = _eod_proposals(db)[0].payload["content"]
    assert set(ID_RE.findall(content)) == CHANGED and content.count("[as recorded]") == len(CHANGED)


def test_the_proposal_records_what_the_agent_proposed_and_what_was_changed(db):
    _run(db)

    proposal = _eod_proposals(db)[0]
    assert sorted(proposal.original_model_output["changed_items"]) == sorted(CHANGED)
    assert {line["reference_id"] for line in proposal.original_model_output["lines"]} == {f"item:{i}" for i in CHANGED}
    assert set(proposal.source_refs) == {f"item:{i}" for i in CHANGED}
    assert proposal.payload["morning_taken_at"] == MORNING.isoformat()


def test_a_brief_and_a_summary_for_the_same_day_do_not_collide(db):
    from pm.eval.pm12_cases import ScriptedGateway
    from pm.jobs.morning_brief_job import run_morning_brief_job

    run_morning_brief_job(_config(), ScriptedGateway(), moment=MORNING, db_path=db, policy=MANUAL)
    _run(db)

    types = sorted(p.type for s in ("pending",) for p in ProposalStore(db).list_by_status(s))
    assert types == ["end_of_day_summary_publish", "morning_brief_publish"]


# --- it goes through the same approval gate -----------------------------------------------------------------------------------


def test_nothing_is_posted_by_proposing(db, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")

    _run(db, publisher=log)

    assert log.read_log() == []


def test_a_person_can_approve_it_and_it_is_posted_once(db, tmp_path):
    _run(db)
    pid = _eod_proposals(db)[0].id
    log = LogPublisher(tmp_path / "log.jsonl")

    outcome = service.approve_and_send(pid, approver_id="sharon.silva", publisher=log, policy=MANUAL, db_path=db)

    assert outcome.outcome == "sent" and len(log.read_log()) == 1
    assert "End-of-day summary — 2026-09-16" in log.read_log()[0]["content"] and "PM-202" in log.read_log()[0]["content"]


def test_a_person_can_reject_it(db):
    _run(db)
    pid = _eod_proposals(db)[0].id

    outcome = service.reject(pid, approver_id="sharon.silva", reason="not today", policy=MANUAL, db_path=db)

    assert outcome.outcome == "rejected" and ProposalStore(db).get(pid).status == REJECTED


def test_a_stranger_cannot_approve_it(db, tmp_path):
    _run(db)
    pid = _eod_proposals(db)[0].id

    outcome = service.approve_and_send(pid, approver_id="mallory", publisher=LogPublisher(tmp_path / "l.jsonl"), policy=MANUAL, db_path=db)

    assert outcome.outcome == "refused" and ProposalStore(db).get(pid).status == PENDING


def test_auto_approve_sends_a_fully_grounded_summary(db, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")

    result = _run(db, policy=ONE_FIRST_HUMAN, publisher=log)

    assert result.delivery_status == AUTO_SENT and len(log.read_log()) == 1


def test_auto_approve_holds_a_summary_where_grounding_dropped_a_line(db, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")
    forged = ScriptedSummaryGateway(tamper=lambda lines: [{**l, "text": l["text"] + " It shipped ahead of schedule."} for l in lines], persistent=True)

    result = _run(db, gateway=forged, policy=ONE_FIRST_HUMAN, publisher=log)

    assert result.delivery_status == PROPOSED and "held for a person" in result.delivery_detail and log.read_log() == []


def test_a_person_approving_a_summary_counts_as_the_first_human_for_the_channel(db, tmp_path):
    """The first-approval rule is about the channel, not the kind of message."""
    _run(db)
    log = LogPublisher(tmp_path / "log.jsonl")
    service.approve_and_send(_eod_proposals(db)[0].id, approver_id="sharon.silva", publisher=log, policy=MANUAL, db_path=db)
    from pm.eval.pm12_cases import ScriptedGateway
    from pm.jobs.morning_brief_job import run_morning_brief_job

    run_morning_brief_job(_config(), ScriptedGateway(), moment=datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc), db_path=db, policy=MANUAL)
    brief = next(p for p in ProposalStore(db).list_by_status("pending") if p.type == "morning_brief_publish")
    auto = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), auto_approve=True, auto_approve_requires_first_human=True)

    why = service.explain_hold(brief.id, policy=auto, publisher=log, db_path=db)

    assert why is None or "first brief" not in why  # a person has already approved something for this channel


def test_the_scheduler_hands_the_end_of_day_job_the_gateway():
    from pm.scheduling.scheduler import build_scheduler

    gateway = ScriptedSummaryGateway()

    scheduler = build_scheduler([_config()], gateway, db_path="x.db")

    job = next(j for j in scheduler.get_jobs() if "end_of_day" in j.id)
    assert job.kwargs["gateway"] is gateway


# --- how a person sees it ----------------------------------------------------------------------------------------------------------


def test_the_pending_list_and_the_card_say_it_is_an_end_of_day_summary(db):
    _run(db)

    (pending,) = [p for p in service.list_pending_approvals(db_path=db) if p.type == EOD_PROPOSAL_TYPE]

    assert pending.summary.startswith("End-of-day summary for 2026-09-16 to ")
    card = pending_brief_card(pending)
    assert "End-of-day summary awaiting approval" in str(card) and any(a["title"] == "Approve" for a in card["actions"])


def test_the_dashboard_offers_approve_and_reject_for_it(db, monkeypatch, tmp_path):
    _run(db)
    pid = _eod_proposals(db)[0].id
    monkeypatch.setenv("PM_DB_PATH", str(db))
    monkeypatch.setenv("P1_DB_PATH", str(tmp_path / "no_p1.db"))
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")
    monkeypatch.setenv("PM_DASHBOARD_USER", "")
    app = str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py")

    at = AppTest.from_file(app, default_timeout=30).run()

    assert not at.exception, [e.value for e in at.exception]
    keys = {b.key for b in at.button}
    assert f"approve_{pid}" in keys and f"reject_{pid}" in keys  # an executable summary: both buttons, unlike a risk proposal
