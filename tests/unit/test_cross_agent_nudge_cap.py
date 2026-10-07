"""One person, one day, two agents: never chased twice. P1's real nudge job and P2's real follow-up, against each other.

Wei Chen is `wei.chen` in P2's roster and a Microsoft Graph user id in P1's. Each agent's cap reads the other's ledger
(P2 reads P1's `nudges`, P1 reads P2's `nudges`), so whichever reminds her first, the second holds back. A control run
without the shared cap shows the double-chase this prevents is real.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timezone

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from p1.config.schema import ChannelConfig
from p1.nudges.nudge_job import CAP_REACHED as P1_CAP_REACHED
from p1.nudges.nudge_job import SENT as P1_SENT
from p1.nudges.nudge_job import run_nudge_job
from p1.participation.ledger import NO_MESSAGE, ParticipationRecord
from p1.storage.db import get_connection, init_db
from p1.storage.nudges_repo import NudgeStore

from pm.approval.service import ApprovalPolicy
from pm.commitments import followup as fu
from pm.commitments.cap import SharedNudgeCap
from pm.commitments.followup import FollowupSettings, run_followups

FRIDAY = date(2026, 9, 18)
P2_MOMENT = datetime(2026, 9, 18, 3, 30, tzinfo=timezone.utc)  # 09:00 Colombo, the same Friday
WEI_IN_P1 = "aad-wei-0001"
SETTINGS = FollowupSettings(
    channel_id="19:proj-gamma@thread.tacv2", timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], non_working_dates=[],
    nudge_days_before_due=1, escalation_threshold_days=3, lead_id="noah.becker", exceptions=frozenset(), cap_per_person_per_day=1,
)


class _P1Publisher:
    def __init__(self):
        self.calls = []

    def post_direct_message(self, member_id, content):
        self.calls.append((member_id, content))
        return {"ok": True}


@pytest.fixture()
def estate(seeded_db_path, tmp_path, monkeypatch):
    """P2's database (the seed), P1's database with Wei under her Graph id, and both agents' environments pointing at each other."""
    p1_db = str(tmp_path / "p1.db")
    init_db(p1_db)
    conn = get_connection(p1_db)
    try:
        conn.execute("INSERT INTO channels (id, display_name, allowlisted) VALUES ('p1-channel', 'P1 channel', 1)")
        conn.execute("INSERT INTO members (id, display_name) VALUES (?, 'Wei Chen')", (WEI_IN_P1,))
        conn.commit()
    finally:
        conn.close()
    # Wei has been reminded before by each agent, so a reminder from either goes out unattended (the steady state).
    NudgeStore(p1_db).record(channel_id="p1-channel", member_id=WEI_IN_P1, date="2026-09-01", idempotency_key="p1-old", proposal_id="x")
    NudgeStore(p1_db).mark_sent(idempotency_key="p1-old", sent_at="2026-09-01T09:00:00+00:00")
    old = SharedNudgeCap(db_path=seeded_db_path, cap=1, p1_db_path=p1_db)
    old.record(member_id="wei.chen", day="2026-09-01", commitment_id=None, proposal_id=None, key="p2-old")
    old.mark_sent("p2-old", sent_at="2026-09-01T03:00:00+00:00", day="2026-09-01")

    monkeypatch.setenv("P1_DB_PATH", p1_db)  # P2 reads P1's ledger from here
    monkeypatch.setenv("P1_SHARED_NUDGE_CAP_PER_DAY", "1")  # P1 reads P2's ledger from here
    monkeypatch.setenv("P1_PEER_NUDGE_LEDGERS", str(seeded_db_path))
    return {"p1": p1_db, "p2": seeded_db_path, "log": LogPublisher(tmp_path / "p2_out.jsonl"), "p1_out": _P1Publisher()}


def _p1_config() -> ChannelConfig:
    return ChannelConfig(
        channel_id="p1-channel", display_name="P1 channel", roster=[WEI_IN_P1, "alice"], update_window_start=time(9, 0),
        update_window_end=time(11, 0), timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], daily_digest_time=time(9, 0),
        weekly_digest_day="Fri", weekly_digest_time=time(16, 0), channel_owner_id="alice", nudge_enabled=True, nudge_cap_per_day=1,
    )


def _p1_nudges_wei(estate):
    config = _p1_config()
    records = [ParticipationRecord(channel_id="p1-channel", member_id=WEI_IN_P1, date=FRIDAY.isoformat(), state=NO_MESSAGE, evidence_message_ids=())]
    results = run_nudge_job("p1-channel", config, estate["p1_out"], day=FRIDAY, db_path=estate["p1"], ledger_records=records)
    return next(r for r in results if r.member_id == WEI_IN_P1)


def _p2_reminds_wei(estate):
    from pm.adapters.tracker import TrackerMock

    items = TrackerMock(db_path=estate["p2"])
    names = {a.id: a.display_name for a in items.list_assignees()}
    results = run_followups(moment=P2_MOMENT, settings=SETTINGS, publisher=estate["log"], db_path=estate["p2"],
                            policy=ApprovalPolicy(approver_ids=frozenset({"sharon.silva"})),
                            item_status=lambda i: items.get_item(i).status, names=names)
    return next(r for r in results if r.commitment_id == 3 and r.action == "nudge")  # #3: Wei, Filter persistence, due tomorrow


def _dms_to_wei(estate):
    p2 = [m for m in estate["log"].read_log() if m["target"] == "wei.chen"]
    p1 = [c for c in estate["p1_out"].calls if c[0] == WEI_IN_P1]
    return p2, p1


def test_when_p2_reminds_her_first_p1_holds_back(estate):
    assert _p2_reminds_wei(estate).status == fu.SENT

    p1_result = _p1_nudges_wei(estate)

    assert p1_result.status == P1_CAP_REACHED and "1 from another agent" in p1_result.detail
    p2_dms, p1_dms = _dms_to_wei(estate)
    assert len(p2_dms) == 1 and p1_dms == []  # chased once, by one agent


def test_when_p1_reminds_her_first_p2_holds_back(estate):
    assert _p1_nudges_wei(estate).status == P1_SENT

    p2_result = _p2_reminds_wei(estate)

    assert p2_result.status == fu.CAP_REACHED and "1 from P1" in p2_result.detail
    p2_dms, p1_dms = _dms_to_wei(estate)
    assert p2_dms == [] and len(p1_dms) == 1


def test_without_the_shared_cap_she_would_be_chased_twice(estate, monkeypatch):
    """The control: switch P1's side off and both agents reminding her the same day is exactly what happens."""
    monkeypatch.delenv("P1_SHARED_NUDGE_CAP_PER_DAY")
    assert _p2_reminds_wei(estate).status == fu.SENT

    assert _p1_nudges_wei(estate).status == P1_SENT  # P1's own cap cannot see P2

    p2_dms, p1_dms = _dms_to_wei(estate)
    assert len(p2_dms) == 1 and len(p1_dms) == 1


def test_the_next_day_they_may_each_remind_her_again(estate):
    assert _p2_reminds_wei(estate).status == fu.SENT
    p2_rows = sqlite3.connect(estate["p2"]).execute("SELECT date, sent_at FROM nudges WHERE member_id = 'wei.chen' AND date = '2026-09-18'").fetchall()
    assert len(p2_rows) == 1  # the ledger P1 reads says: one on the 18th

    monday = date(2026, 9, 21)
    config = _p1_config()
    records = [ParticipationRecord(channel_id="p1-channel", member_id=WEI_IN_P1, date=monday.isoformat(), state=NO_MESSAGE, evidence_message_ids=())]
    result = next(r for r in run_nudge_job("p1-channel", config, estate["p1_out"], day=monday, db_path=estate["p1"], ledger_records=records)
                  if r.member_id == WEI_IN_P1)

    assert result.status == P1_SENT  # a different day: the cap is per day


def test_someone_else_is_unaffected_by_weis_reminder(estate):
    assert _p2_reminds_wei(estate).status == fu.SENT
    conn = get_connection(estate["p1"])
    try:
        conn.execute("INSERT INTO members (id, display_name) VALUES ('alice', 'Alice Other')")
        conn.commit()
    finally:
        conn.close()
    NudgeStore(estate["p1"]).record(channel_id="p1-channel", member_id="alice", date="2026-09-01", idempotency_key="a-old", proposal_id="x")
    NudgeStore(estate["p1"]).mark_sent(idempotency_key="a-old", sent_at="2026-09-01T09:00:00+00:00")
    config = _p1_config()
    records = [ParticipationRecord(channel_id="p1-channel", member_id="alice", date=FRIDAY.isoformat(), state=NO_MESSAGE, evidence_message_ids=())]

    results = run_nudge_job("p1-channel", config, estate["p1_out"], day=FRIDAY, db_path=estate["p1"], ledger_records=records)

    assert next(r for r in results if r.member_id == "alice").status == P1_SENT
