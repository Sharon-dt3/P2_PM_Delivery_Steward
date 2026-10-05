"""The Streamlit approval dashboard, driven headlessly (streamlit.testing.v1.AppTest)
the way P1's is: the real widgets, clicked as a person would.

The page is thin on purpose. It calls the same service functions as the command
line and the Teams card, so it can do nothing they cannot: the rules (who may
approve, scope, edits keep the original, everything audited) live in
pm.approval.service. These tests prove the page wires them correctly.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from p1.adapters.teams_publisher import TeamsPublisher
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import APPLIED, APPROVED, PENDING, REJECTED, ProposalStore
from streamlit.testing.v1 import AppTest

from pm.approval.audit import audit_trail
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

APP_PATH = str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py")
WED = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch, seeded_db_path, tmp_path):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("P1_DB_PATH", str(tmp_path / "no_p1.db"))  # never fall back to the real P1 database
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva,noah.becker")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")  # set, not unset: the app's load_dotenv() would otherwise take it from the real .env
    monkeypatch.setenv("PM_DASHBOARD_USER", "")


@pytest.fixture()
def log(tmp_path):
    return LogPublisher(tmp_path / "log.jsonl")


def _propose(db, moment=WED):
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    return run_morning_brief_job(config, ScriptedGateway(), moment=moment, db_path=db, policy=_no_auto()).proposal_id


def _no_auto():
    from pm.approval.service import ApprovalPolicy

    return ApprovalPolicy()


def _app() -> AppTest:
    return AppTest.from_file(APP_PATH, default_timeout=30).run()


def _text(at: AppTest) -> str:
    parts = []
    for kind in ("markdown", "caption", "text", "info", "success", "warning", "error", "title", "header", "subheader"):
        parts += [str(el.value) for el in getattr(at, kind)]
    return "\n".join(parts)


def _no_exception(at: AppTest):
    assert not at.exception, [e.value for e in at.exception]


# --- what it shows ---------------------------------------------------------------------


def test_with_nothing_pending_it_says_so(seeded_db_path):
    at = _app()

    _no_exception(at)
    assert "Nothing is awaiting approval" in _text(at)


def test_it_lists_a_pending_brief_with_its_text(seeded_db_path):
    proposal_id = _propose(seeded_db_path)

    at = _app()

    _no_exception(at)
    text = _text(at)
    assert "2026-09-16" in text and CHANNEL_ID in text and proposal_id[:8] in text
    assert "Morning brief — 2026-09-16" in text  # the exact text that would be posted
    assert at.button(key=f"approve_{proposal_id}") is not None and at.button(key=f"reject_{proposal_id}") is not None


def test_it_says_what_is_switched_on_so_nobody_is_surprised(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_AUTO_APPROVE", "1")

    at = _app()

    text = _text(at)
    assert "log-only" in text and "auto-approve: ON" in text and "sharon.silva" in text


def test_it_never_shows_the_flow_url(seeded_db_path, monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "power_automate")
    monkeypatch.setenv("POWER_AUTOMATE_FLOW_URL", "https://example.invalid/secret-flow?sig=abc123")

    at = _app()

    assert "secret-flow" not in _text(at) and "abc123" not in _text(at) and "REAL" in _text(at)


# --- who is acting ---------------------------------------------------------------------


def test_you_can_only_act_as_a_configured_approver(seeded_db_path):
    at = _app()

    assert list(at.selectbox(key="acting_as").options) == ["noah.becker", "sharon.silva"]
    assert "system:auto-approve" not in at.selectbox(key="acting_as").options


def test_the_default_user_can_be_set_and_must_be_an_approver(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_DASHBOARD_USER", "sharon.silva")
    assert _app().selectbox(key="acting_as").value == "sharon.silva"

    monkeypatch.setenv("PM_DASHBOARD_USER", "mallory")
    assert _app().selectbox(key="acting_as").value in ("noah.becker", "sharon.silva")


def test_with_no_approvers_configured_there_are_no_buttons(seeded_db_path, monkeypatch):
    proposal_id = _propose(seeded_db_path)
    monkeypatch.setenv("PM_APPROVER_IDS", "")

    at = _app()

    _no_exception(at)
    assert "PM_APPROVER_IDS" in _text(at) and "Morning brief — 2026-09-16" in _text(at)  # still readable
    assert not [b for b in at.button if b.key and b.key.startswith(("approve_", "reject_"))]
    assert ProposalStore(seeded_db_path).get(proposal_id).status == PENDING


# --- approve, edit, reject -------------------------------------------------------------


def test_clicking_approve_sends_it_and_records_who(seeded_db_path, log):
    proposal_id = _propose(seeded_db_path)
    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()

    at.button(key=f"approve_{proposal_id}").click().run()

    _no_exception(at)
    proposal = ProposalStore(seeded_db_path).get(proposal_id)
    assert proposal.status == APPLIED and proposal.approver_id == "sharon.silva"
    assert len(log.read_log()) == 1
    assert "sent" in _text(at).lower() and "Nothing is awaiting approval" in _text(at)


def test_editing_the_text_then_approving_sends_the_edit_and_keeps_the_original(seeded_db_path, log):
    proposal_id = _propose(seeded_db_path)
    original = ProposalStore(seeded_db_path).get(proposal_id).payload["content"]
    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()

    at.text_area(key=f"edit_{proposal_id}").input("Shorter brief for today.").run()
    assert "edited" in _text(at).lower()  # the page says the edit will be what is sent
    at.button(key=f"approve_{proposal_id}").click().run()

    _no_exception(at)
    assert log.read_log()[0]["content"] == "Shorter brief for today."
    trail = audit_trail(proposal_id, db_path=seeded_db_path)
    assert trail.edited and trail.original_proposal["content"] == original


def test_an_unedited_approval_is_not_recorded_as_an_edit(seeded_db_path, log):
    proposal_id = _propose(seeded_db_path)
    at = _app()
    at.selectbox(key="acting_as").select("noah.becker").run()

    at.button(key=f"approve_{proposal_id}").click().run()

    trail = audit_trail(proposal_id, db_path=seeded_db_path)
    assert trail.edited is False and trail.approver_id == "noah.becker"


def test_clicking_reject_rejects_with_the_reason(seeded_db_path, log):
    proposal_id = _propose(seeded_db_path)
    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()

    at.text_input(key=f"reason_{proposal_id}").input("figures look stale").run()
    at.button(key=f"reject_{proposal_id}").click().run()

    _no_exception(at)
    assert ProposalStore(seeded_db_path).get(proposal_id).status == REJECTED and log.read_log() == []
    rejected = next(e for e in audit_trail(proposal_id, db_path=seeded_db_path).events if e["action"] == "proposal.rejected")
    assert rejected["details"]["reason"] == "figures look stale"


# --- the audit view --------------------------------------------------------------------


def test_the_audit_view_shows_who_when_and_the_original_versus_applied(seeded_db_path, log):
    proposal_id = _propose(seeded_db_path)
    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()
    at.text_area(key=f"edit_{proposal_id}").input("Edited for the team.").run()
    at.button(key=f"approve_{proposal_id}").click().run()

    at = _app()
    at.selectbox(key="audit_pick").select(proposal_id).run()

    _no_exception(at)
    text = _text(at)
    assert "Approved by sharon.silva" in text and "with edits" in text
    assert "The agent originally proposed" in text and "Morning brief — 2026-09-16" in text
    assert "Edited for the team." in text
    assert "proposal.created" in text and "proposal.approved" in text and "proposal.sent" in text


def test_the_audit_view_marks_an_automatic_approval(seeded_db_path, log, monkeypatch):
    from pm.approval.service import ApprovalPolicy, approve_and_send

    policy = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), auto_approve=True)
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    first = run_morning_brief_job(config, ScriptedGateway(), moment=datetime(2026, 9, 14, 2, 30, tzinfo=timezone.utc),
                                  db_path=seeded_db_path, publisher=log, policy=policy).proposal_id
    approve_and_send(first, approver_id="sharon.silva", publisher=log, policy=policy, db_path=seeded_db_path)
    second = run_morning_brief_job(config, ScriptedGateway(), moment=datetime(2026, 9, 15, 2, 30, tzinfo=timezone.utc),
                                   db_path=seeded_db_path, publisher=log, policy=policy).proposal_id

    at = _app()
    at.selectbox(key="audit_pick").select(second).run()

    assert "Automatically approved by the system" in _text(at) and "no person reviewed it" in _text(at)


def test_the_audit_view_lists_pending_ones_too(seeded_db_path):
    proposal_id = _propose(seeded_db_path)

    at = _app()
    at.selectbox(key="audit_pick").select(proposal_id).run()

    assert "Awaiting a decision" in _text(at)


# --- an approved brief whose send failed -----------------------------------------------


class _Down(TeamsPublisher):
    def post_channel_message(self, channel_id, content):
        raise ConnectionError("flow unreachable")

    def post_direct_message(self, member_id, content):
        raise AssertionError


def test_a_failed_send_can_be_retried_from_the_page(seeded_db_path, log, monkeypatch):
    from pm.approval import service

    proposal_id = _propose(seeded_db_path)
    monkeypatch.setattr(service, "get_teams_publisher", lambda: _Down())
    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()
    # a real-kind publisher needs an allowlisted target: aim the proposal at one
    store = ProposalStore(seeded_db_path)
    payload = {**store.get(proposal_id).payload, "target_channel": "19:test@thread.tacv2"}
    import sqlite3

    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (__import__("json").dumps(payload), proposal_id))
    conn.commit()
    conn.close()
    monkeypatch.setattr(service, "load_approval_policy", lambda *a, **k: service.ApprovalPolicy(
        approver_ids=frozenset({"sharon.silva"}), allowlisted_channel_ids=["19:test@thread.tacv2"]))

    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()
    at.button(key=f"approve_{proposal_id}").click().run()
    assert ProposalStore(seeded_db_path).get(proposal_id).status == APPROVED

    monkeypatch.setattr(service, "get_teams_publisher", lambda: log)
    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()
    at.button(key=f"retry_{proposal_id}").click().run()

    assert ProposalStore(seeded_db_path).get(proposal_id).status == APPLIED and len(log.read_log()) == 1
