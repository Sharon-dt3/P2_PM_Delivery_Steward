"""The dashboard shows people and channels by NAME, never by raw id.

An approver has to see WHO a nudge is about and WHERE a message goes: a Teams
channel id or an Azure AD guid tells them nothing. Names come from P1's own
sources (its member table and its channel config). When a name genuinely cannot
be found the id is shown rather than hidden, so nothing is ever concealed.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from p1.approval.proposals import ProposalStore as P1ProposalStore
from p1.storage.db import init_db as p1_init_db
from streamlit.testing.v1 import AppTest

from pm.approval.service import ApprovalPolicy
from pm.dashboard.names import channel_name, member_name
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

APP_PATH = str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py")
P1_CHANNEL = "19:ZVl0BYQCKWi4_oXsG_tuu3F4p5HsgQGobGhAMiZD_ro1@thread.tacv2"  # p1-agent-test
OTHER_CHANNEL = "19:ID3C8qqqxb40IRhNJ3xvts2BWAgRac3SxYwm9XyBEGM1@thread.tacv2"  # Teams-agent-test
MEMBER = "442e9b43-e50a-446c-9c4b-e612e9036bd8"
OWNER = "a52e61e5-16c6-4f6c-af67-f41f85e7a00a"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
THREAD_ID = re.compile(r"19:[A-Za-z0-9_\-]+@thread\.\w+")


@pytest.fixture(autouse=True)
def _env(monkeypatch, seeded_db_path, tmp_path):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")
    monkeypatch.setenv("PM_DASHBOARD_USER", "")


@pytest.fixture()
def p1_db(tmp_path, monkeypatch):
    path = tmp_path / "p1_test.db"
    p1_init_db(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO members (id, display_name) VALUES (?, ?)",
        [(MEMBER, "Himanshu Ranjan"), (OWNER, "Sharon Silva")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("P1_DB_PATH", str(path))
    return path


def _nudge(db, *, member=MEMBER, channel=P1_CHANNEL, key="n1"):
    return P1ProposalStore(db).create(
        type="nudge",
        payload={"channel_id": channel, "member_id": member, "date": "2026-10-03", "state": "x",
                 "content": "Hi! We haven't seen a message from you in p1-agent-test today."},
        original_model_output={}, source_refs=[], idempotency_key=key,
    )


def _escalation(db, *, member=MEMBER, channel=P1_CHANNEL, key="e1"):
    return P1ProposalStore(db).create(
        type="escalation",
        payload={"channel_id": channel, "member_id": member, "days": 4, "streak_start_date": "2026-09-29",
                 "streak_end_date": "2026-10-02", "content": "Hi! Himanshu Ranjan has missed 4 working days."},
        original_model_output={}, source_refs=[], idempotency_key=key,
    )


def _text(at: AppTest) -> str:
    parts = []
    for kind in ("markdown", "caption", "text", "info", "success", "warning", "error", "title", "header", "subheader"):
        parts += [str(el.value) for el in getattr(at, kind)]
    return "\n".join(parts)


def _app() -> AppTest:
    return AppTest.from_file(APP_PATH, default_timeout=30).run()


# --- the lookups -----------------------------------------------------------------------------------


def test_a_channel_id_becomes_its_display_name(p1_db):
    assert channel_name(P1_CHANNEL, p1_db) == "p1-agent-test"
    assert channel_name(OTHER_CHANNEL, p1_db) == "Teams-agent-test"
    assert channel_name(CHANNEL_ID, p1_db) == "Project Gamma"  # P2's own seeded channel, from P1's channel config


def test_a_member_id_becomes_the_members_name(p1_db):
    assert member_name(MEMBER, p1_db) == "Himanshu Ranjan"


def test_an_unknown_id_is_shown_as_is_not_hidden(p1_db):
    assert channel_name("19:unknown@thread.tacv2", p1_db) == "19:unknown@thread.tacv2"
    assert member_name("not-a-known-member", p1_db) == "not-a-known-member"


def test_lookups_survive_a_missing_p1_database(tmp_path):
    missing = tmp_path / "nope.db"

    assert channel_name(P1_CHANNEL, missing) == "p1-agent-test"  # channel names come from P1's config files
    assert member_name(MEMBER, missing) == MEMBER
    assert not missing.exists()


# --- the P1 tab ----------------------------------------------------------------------------------------


def test_a_nudge_shows_who_it_is_about_by_name_and_no_raw_ids(seeded_db_path, p1_db):
    _nudge(p1_db)

    text = _text(_app())

    assert "Himanshu Ranjan" in text and "p1-agent-test" in text
    assert not GUID.search(text), GUID.search(text)
    assert not any(t for t in THREAD_ID.findall(text) if t != CHANNEL_ID)  # no raw P1 channel ids anywhere


def test_an_escalation_shows_the_person_and_who_receives_it(seeded_db_path, p1_db, monkeypatch):
    _escalation(p1_db)

    text = _text(_app())

    assert "Himanshu Ranjan" in text and "p1-agent-test" in text
    assert "Goes to" in text  # the recipient is shown by name too
    assert not GUID.search(text)


def test_the_summary_line_is_rewritten_with_names(seeded_db_path, p1_db):
    _nudge(p1_db)
    _escalation(p1_db)

    text = _text(_app())

    assert "Nudge Himanshu Ranjan in p1-agent-test" in text
    assert "Escalate Himanshu Ranjan in p1-agent-test" in text


def test_a_digest_goes_to_the_named_channel(seeded_db_path, p1_db):
    P1ProposalStore(p1_db).create(
        type="daily_digest_publish",
        payload={"channel_id": OTHER_CHANNEL, "target_channel": OTHER_CHANNEL, "date": "2026-10-03", "content": "Digest text"},
        original_model_output={}, source_refs=[], idempotency_key="d1",
    )

    text = _text(_app())

    assert "Teams-agent-test" in text and OTHER_CHANNEL not in text


def test_a_member_with_no_known_name_is_still_shown_by_id(seeded_db_path, p1_db):
    _nudge(p1_db, member="ffffffff-0000-0000-0000-000000000000", key="n2")

    assert "ffffffff-0000-0000-0000-000000000000" in _text(_app())


# --- the P2 tab ----------------------------------------------------------------------------------------------


def test_the_p2_tab_names_the_channel_too(seeded_db_path, p1_db):
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    run_morning_brief_job(
        config, ScriptedGateway(), moment=datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc),
        db_path=seeded_db_path, policy=ApprovalPolicy(),
    )

    at = _app()

    headings = [str(el.value) for el in at.subheader]
    assert any("Project Gamma" in h and "2026-09-16" in h for h in headings), headings
    assert not any(CHANNEL_ID in h for h in headings)  # the heading shows the name, not the id
