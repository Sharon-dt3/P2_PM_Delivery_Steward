"""PM-24: the commitment pass in the morning job, the command line, and the real author lookup for outcome records."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from p1.contracts.outcome_record import EvidenceItem, OutcomeRecord, write_outcome

from pm.approval.service import ApprovalPolicy
from pm.commitments.job import run_commitment_pass_safely
from pm.commitments.outcomes import ADDED, ingest_recent_outcomes, message_lookup
from pm.commitments.store import CommitmentTracker
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

REPO = Path(__file__).resolve().parents[2]
FRI = datetime(2026, 9, 18, 3, 30, tzinfo=timezone.utc)
MANUAL = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))


@pytest.fixture()
def p1_ledger(tmp_path, monkeypatch):
    path = tmp_path / "p1.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nudges (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL, member_id TEXT NOT NULL, "
                 "date TEXT NOT NULL, proposal_id TEXT, sent_at TEXT, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("P1_DB_PATH", str(path))
    return path


def _config():
    return ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                 morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))


def _morning(db, log):
    return run_morning_brief_job(_config(), ScriptedGateway(), moment=FRI, db_path=db, publisher=log, policy=MANUAL)


# --- the morning job -----------------------------------------------------------------------------------------------------------------


def test_the_morning_job_does_not_follow_up_commitments_unless_switched_on(seeded_db_path, tmp_path, p1_ledger):
    log = LogPublisher(tmp_path / "log.jsonl")

    result = _morning(seeded_db_path, log)

    assert result.commitment_followups == 0 and log.read_log() == []
    assert sqlite3.connect(seeded_db_path).execute("SELECT count(*) FROM commitment_events").fetchone()[0] == 0


def test_when_switched_on_the_morning_job_follows_up_after_the_brief(seeded_db_path, tmp_path, p1_ledger, monkeypatch):
    monkeypatch.setenv("PM_COMMITMENT_FOLLOWUP", "1")
    log = LogPublisher(tmp_path / "log.jsonl")

    result = _morning(seeded_db_path, log)

    assert result.status == "generated" and result.proposal_id  # the brief is unaffected
    assert result.commitment_followups >= 1  # first reminders to mateo, wei and noah are waiting for a person
    tracker = CommitmentTracker(seeded_db_path)
    assert all(tracker.event(c, "overdue") for c in (5, 6, 7, 8))  # and the overdue ones are on record
    assert log.read_log() == []  # nothing has been sent: every first reminder needs a person


def test_a_failure_in_the_follow_up_never_stops_the_brief(seeded_db_path, tmp_path, p1_ledger, monkeypatch):
    monkeypatch.setenv("PM_COMMITMENT_FOLLOWUP", "1")
    from pm.commitments import job

    monkeypatch.setattr(job, "run_commitment_pass", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("config unreadable")))

    result = _morning(seeded_db_path, LogPublisher(tmp_path / "log.jsonl"))

    assert result.status == "generated" and result.proposal_id and result.commitment_followups == 0


def test_an_unreadable_p1_ledger_stops_the_reminders_but_not_the_brief(seeded_db_path, tmp_path, p1_ledger, monkeypatch):
    monkeypatch.setenv("PM_COMMITMENT_FOLLOWUP", "1")
    p1_ledger.write_text("not a database")
    log = LogPublisher(tmp_path / "log.jsonl")

    result = _morning(seeded_db_path, log)

    assert result.status == "generated" and result.commitment_followups == 0 and log.read_log() == []
    assert not sqlite3.connect(seeded_db_path).execute("SELECT count(*) FROM proposals WHERE type = 'commitment_nudge'").fetchone()[0]


def test_the_safe_wrapper_is_off_when_not_enabled(seeded_db_path, p1_ledger):
    assert run_commitment_pass_safely(FRI, db_path=seeded_db_path) == 0


# --- the author of a line in an outcome record, from the channel's own messages -------------------------------------------------------


def test_the_author_of_a_message_is_found_from_the_channels_messages(seeded_db_path):
    lookup = message_lookup(CHANNEL_ID, seeded_db_path)

    info = lookup("proj-gamma-0195")

    assert info is not None and info.author_id == "olivia.dupont" and info.posted_at.startswith("2025-06-12")
    assert lookup("no-such-message") is None


def test_a_real_channel_message_that_makes_a_promise_becomes_a_commitment(seeded_db_path):
    """proj-gamma-0195 is a real message in the channel fixture: 'Started work on the export job today, will have an update tomorrow.'"""
    record = OutcomeRecord(
        schema_version="1.0", channel_id=CHANNEL_ID, channel_display_name="Project Gamma", date=date(2025, 6, 12), allowlisted=True,
        roster=[], updates=[EvidenceItem(message_id="proj-gamma-0195", text="Started work on the export job today, will have an update tomorrow.",
                                         quote="will have an update tomorrow")],
        participation=[], generated_at="2025-06-12T22:00:00+00:00")
    from pm.commitments.outcomes import ingest_outcome_record

    tracker = CommitmentTracker(seeded_db_path)
    (result,) = ingest_outcome_record(record, tracker=tracker, message_info=message_lookup(CHANNEL_ID, seeded_db_path),
                                      roster={"olivia.dupont", "wei.chen"})

    commitment = tracker.get(result.commitment_id)
    assert result.status == ADDED and commitment.member_id == "olivia.dupont"
    assert (commitment.made_at, commitment.due_date_iso, commitment.due_date_text) == ("2025-06-12", "2025-06-13", "tomorrow")


def test_recent_outcome_records_are_read_from_p1s_output_folder(seeded_db_path, tmp_path):
    folder = tmp_path / "outcomes"
    for day, mid in ((date(2026, 9, 17), "m-a"), (date(2026, 9, 18), "m-b")):
        write_outcome(OutcomeRecord(
            schema_version="1.0", channel_id=CHANNEL_ID, channel_display_name="Project Gamma", date=day, allowlisted=True, roster=[],
            updates=[EvidenceItem(message_id=mid, text="I'll have the fix done by 2026-09-24.", quote="I'll have the fix")],
            participation=[], generated_at="2026-09-18T22:00:00+00:00"), output_dir=folder)
    from pm.commitments.outcomes import MessageInfo

    info = {"m-a": MessageInfo("wei.chen", "2026-09-17T09:00:00"), "m-b": MessageInfo("noah.becker", "2026-09-18T09:00:00")}
    tracker = CommitmentTracker(seeded_db_path)

    first = ingest_recent_outcomes(channel_id=CHANNEL_ID, today=date(2026, 9, 18), days_back=7, output_dir=folder, tracker=tracker,
                                   message_info=info.get, roster={"wei.chen", "noah.becker"})
    again = ingest_recent_outcomes(channel_id=CHANNEL_ID, today=date(2026, 9, 18), days_back=7, output_dir=folder, tracker=tracker,
                                   message_info=info.get, roster={"wei.chen", "noah.becker"})

    assert [r.status for r in first] == [ADDED, ADDED] and [r.status for r in again] == ["already_known", "already_known"]
    assert len(tracker.list()) == 10


def test_a_day_with_no_outcome_record_is_skipped(seeded_db_path, tmp_path):
    results = ingest_recent_outcomes(channel_id=CHANNEL_ID, today=date(2026, 9, 18), days_back=3, output_dir=tmp_path / "nothing",
                                     tracker=CommitmentTracker(seeded_db_path), message_info=lambda _id: None, roster=set())

    assert results == []


# --- the command line ------------------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def cli():
    spec = importlib.util.spec_from_file_location("commitments_script", REPO / "scripts" / "commitments.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ageing_command_prints_the_view(cli, seeded_db_path, capsys):
    code = cli.main(["--db", str(seeded_db_path), "ageing", "--at", "2026-09-18T09:00"])

    out = capsys.readouterr().out
    assert code == 0 and out.startswith("Open commitments as of 2026-09-18: 8") and "overdue 3 to 7 days" in out and "Olivia Dupree" in out


def test_a_dry_run_changes_and_sends_nothing(cli, seeded_db_path, capsys, p1_ledger, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")
    before = sqlite3.connect(seeded_db_path).execute("SELECT count(*) FROM commitment_events").fetchone()[0]

    code = cli.main(["--db", str(seeded_db_path), "run", "--at", "2026-09-18T09:00", "--dry-run"], publisher=log)

    out = capsys.readouterr().out
    assert code == 0 and "DRY RUN" in out and "would_await_approval" in out and "overdue" in out and "escalate beyond 3 days" in out
    assert log.read_log() == [] and sqlite3.connect(seeded_db_path).execute("SELECT count(*) FROM commitment_events").fetchone()[0] == before


def test_a_real_run_records_what_it_did_and_says_who_must_approve(cli, seeded_db_path, capsys, p1_ledger, tmp_path):
    code = cli.main(["--db", str(seeded_db_path), "run", "--at", "2026-09-18T09:00"], publisher=LogPublisher(tmp_path / "log.jsonl"))

    out = capsys.readouterr().out
    assert code == 0 and "awaiting_approval" in out and "approve.py list" in out
    assert CommitmentTracker(seeded_db_path).event(6, "overdue")


def test_closing_a_commitment_by_hand(cli, seeded_db_path, capsys):
    assert cli.main(["--db", str(seeded_db_path), "close", "6"]) == 0
    assert "closed" in capsys.readouterr().out and CommitmentTracker(seeded_db_path).get(6).status == "fulfilled"

    assert cli.main(["--db", str(seeded_db_path), "close", "6"]) == 1  # already closed: nothing changes
    assert cli.main(["--db", str(seeded_db_path), "close", "999"]) == 1 and "No commitment #999" in capsys.readouterr().out


def test_ingesting_the_committed_fixtures_by_command_adds_nothing(cli, seeded_db_path, capsys):
    folder = REPO / "src" / "pm" / "seed" / "fixtures" / "outcomes" / "19_proj-gamma_thread.tacv2"

    code = cli.main(["--db", str(seeded_db_path), "ingest", str(folder)])

    out = capsys.readouterr().out
    assert code == 0 and "0 commitment(s) added" in out and "consent_withheld" in out
