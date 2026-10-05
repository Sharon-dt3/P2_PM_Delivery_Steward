"""The combined approvals dashboard: one page, one sign-in, a tab for each project.

The P2 tab is the P2 approval page (its own tests are in
test_approval_dashboard_app.py). The P1 tab is READ-ONLY: it lists what P1 has
waiting, through P1's own approval service and its own database, and offers no
button -- P1's approver rules live in P1's repo and are not changed from here.
Nothing the hub does may write to P1's database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from p1.approval.proposals import ProposalStore as P1ProposalStore
from p1.storage.db import init_db as p1_init_db
from spine.approval.proposals import ProposalStore
from streamlit.testing.v1 import AppTest

from pm.approval.service import ApprovalPolicy
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

APP_PATH = str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py")
WED = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)
P1_CHANNEL = "19:ZVl0BYQCKWi4_oXsG_tuu3F4p5HsgQGobGhAMiZD_ro1@thread.tacv2"  # p1-agent-test, a real P1 channel


@pytest.fixture(autouse=True)
def _env(monkeypatch, seeded_db_path, tmp_path):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")  # set, not unset: the app's load_dotenv() would otherwise take it from the real .env
    monkeypatch.setenv("PM_DASHBOARD_USER", "")


@pytest.fixture()
def p1_db(tmp_path, monkeypatch):
    path = tmp_path / "p1_test.db"
    p1_init_db(path)
    monkeypatch.setenv("P1_DB_PATH", str(path))
    return path


def _p1_proposal(db, *, key, kind="daily_digest_publish", content="P1 daily summary text", date="2026-10-03"):
    return P1ProposalStore(db).create(
        type=kind,
        payload={"channel_id": P1_CHANNEL, "target_channel": P1_CHANNEL, "date": date, "content": content},
        original_model_output={"content": content},
        source_refs=[],
        idempotency_key=key,
    )


def _p2_propose(db):
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    return run_morning_brief_job(config, ScriptedGateway(), moment=WED, db_path=db, policy=ApprovalPolicy()).proposal_id


def _app() -> AppTest:
    return AppTest.from_file(APP_PATH, default_timeout=30).run()


def _text(at: AppTest) -> str:
    parts = []
    for kind in ("markdown", "caption", "text", "info", "success", "warning", "error", "title", "header", "subheader", "metric"):
        parts += [str(getattr(el, "value", "")) + " " + str(getattr(el, "label", "")) for el in getattr(at, kind)]
    return "\n".join(parts)


def _p1_rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT id, status, approver_id, decided_at FROM proposals ORDER BY id").fetchall()
    finally:
        conn.close()


# --- one page, both projects --------------------------------------------------------------------


def test_the_header_counts_what_is_waiting_in_each_project(seeded_db_path, p1_db):
    _p2_propose(seeded_db_path)
    _p1_proposal(p1_db, key="a")
    _p1_proposal(p1_db, key="b", kind="nudge", content="nudge text")

    at = _app()

    assert not at.exception, [e.value for e in at.exception]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["P2: waiting"] == "1" and metrics["P1: waiting"] == "2"


def test_there_is_a_tab_for_each_project(seeded_db_path, p1_db):
    at = _app()

    assert [t.label for t in at.tabs] == ["P2 morning brief", "P1 channel"]


def test_the_p1_tab_lists_p1s_pending_items_with_their_text(seeded_db_path, p1_db):
    _p1_proposal(p1_db, key="a", content="P1 daily summary text for the channel")

    at = _app()

    text = _text(at)
    assert "daily_digest_publish" in text and "P1 daily summary text for the channel" in text and "2026-10-03" in text


def test_the_two_projects_items_stay_in_their_own_tabs(seeded_db_path, p1_db):
    p2_id = _p2_propose(seeded_db_path)
    p1_id = _p1_proposal(p1_db, key="a").id

    at = _app()

    keys = {b.key for b in at.button}
    assert f"approve_{p2_id}" in keys
    assert not any(p1_id in (k or "") for k in keys)  # the P1 tab offers no buttons


def test_the_p1_tab_is_read_only_and_says_where_to_approve(seeded_db_path, p1_db):
    _p1_proposal(p1_db, key="a")

    at = _app()

    assert "read-only" in _text(at).lower() and "approval_dashboard.py" in _text(at)


def test_using_the_page_never_writes_to_p1s_database(seeded_db_path, p1_db):
    p2_id = _p2_propose(seeded_db_path)
    _p1_proposal(p1_db, key="a")
    before = _p1_rows(p1_db)

    at = _app()
    at.selectbox(key="acting_as").select("sharon.silva").run()
    at.button(key=f"approve_{p2_id}").click().run()

    assert _p1_rows(p1_db) == before  # still pending, nobody recorded
    assert ProposalStore(seeded_db_path).get(p2_id).status == "applied"


def test_acting_as_still_comes_from_the_p2_approvers(seeded_db_path, p1_db):
    at = _app()

    assert list(at.selectbox(key="acting_as").options) == ["sharon.silva"]


# --- when P1 is not there --------------------------------------------------------------------------


def test_a_missing_p1_database_does_not_break_the_page(seeded_db_path, monkeypatch, tmp_path):
    p2_id = _p2_propose(seeded_db_path)
    monkeypatch.setenv("P1_DB_PATH", str(tmp_path / "nope.db"))

    at = _app()

    assert not at.exception, [e.value for e in at.exception]
    assert "P1 database not found" in _text(at)
    assert f"approve_{p2_id}" in {b.key for b in at.button}
    assert not (tmp_path / "nope.db").exists()  # looking must not create it


def test_a_p1_database_without_the_tables_is_reported_not_raised(seeded_db_path, monkeypatch, tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    monkeypatch.setenv("P1_DB_PATH", str(empty))

    at = _app()

    assert not at.exception, [e.value for e in at.exception]
    assert "could not read" in _text(at).lower()


def test_an_empty_p1_queue_says_so(seeded_db_path, p1_db):
    at = _app()

    assert "Nothing is waiting in P1" in _text(at)


# --- why is it waiting -------------------------------------------------------------------------------


def test_a_waiting_brief_says_why_when_auto_approve_is_off(seeded_db_path, p1_db):
    _p2_propose(seeded_db_path)

    assert "auto-approve is off" in _text(_app()).lower()


def test_a_waiting_brief_says_why_when_it_is_waiting_for_a_first_approval(seeded_db_path, p1_db, monkeypatch):
    monkeypatch.setenv("PM_AUTO_APPROVE", "1")
    _p2_propose(seeded_db_path)

    assert "first brief for this channel" in _text(_app()).lower()
