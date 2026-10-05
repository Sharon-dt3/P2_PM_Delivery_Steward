"""The runner: one place that actually starts P2's schedule, or runs the morning
job once at a chosen moment (the clock override) so a real proposal exists.

Nothing here decides what is sent: the job only proposes, and the approval
service (or auto-approve, if switched on) decides the rest.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from spine.approval.proposals import PENDING, ProposalStore

from pm.approval.service import list_pending_approvals


@pytest.fixture()
def runner():
    path = Path(__file__).parents[2] / "scripts" / "run_scheduler.py"
    spec = importlib.util.spec_from_file_location("run_scheduler_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.delenv("PM_AUTO_APPROVE", raising=False)
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")


def test_at_is_read_in_the_projects_own_timezone(runner):
    moment = runner.parse_at("2026-09-16T08:00", "Asia/Colombo")

    assert moment == datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)


def test_a_bad_at_value_is_an_error_not_a_crash(runner, capsys):
    code = runner.main(["--once", "--at", "next tuesday-ish", "--gateway", "scripted"])

    assert code == 2 and "--at" in capsys.readouterr().out


def test_once_runs_the_morning_job_at_the_chosen_moment_and_leaves_a_pending_proposal(runner, seeded_db_path, capsys):
    code = runner.main(["--db", str(seeded_db_path), "--once", "--at", "2026-09-16T08:00", "--gateway", "scripted"])

    out = capsys.readouterr().out
    assert code == 0 and "proposed" in out
    (pending,) = list_pending_approvals(db_path=seeded_db_path)
    assert pending.local_date == "2026-09-16"
    assert ProposalStore(seeded_db_path).get(pending.proposal_id).status == PENDING
    assert pending.proposal_id in out


def test_once_on_a_weekend_is_skipped_and_proposes_nothing(runner, seeded_db_path, capsys):
    code = runner.main(["--db", str(seeded_db_path), "--once", "--at", "2026-09-19T08:00", "--gateway", "scripted"])

    assert code == 0 and "skipped" in capsys.readouterr().out
    assert list_pending_approvals(db_path=seeded_db_path) == []


def test_once_twice_for_the_same_day_does_not_make_a_second_proposal(runner, seeded_db_path, capsys):
    args = ["--db", str(seeded_db_path), "--once", "--at", "2026-09-16T08:00", "--gateway", "scripted"]
    runner.main(args)
    runner.main(args)

    assert len(list_pending_approvals(db_path=seeded_db_path)) == 1
    assert "already_proposed" in capsys.readouterr().out


def test_print_schedule_lists_both_jobs_and_starts_nothing(runner, seeded_db_path, capsys):
    code = runner.main(["--db", str(seeded_db_path), "--print-schedule", "--gateway", "scripted"])

    out = capsys.readouterr().out
    assert code == 0
    assert "morning_brief" in out and "end_of_day" in out and "Asia/Colombo" in out
    assert "08:00" in out and "17:00" in out and "Mon" in out
    assert list_pending_approvals(db_path=seeded_db_path) == []


def test_serve_starts_the_scheduler_with_both_jobs_and_stops_cleanly(runner, seeded_db_path, capsys):
    code = runner.main(["--db", str(seeded_db_path), "--gateway", "scripted"], block=False)

    assert code == 0
    out = capsys.readouterr().out
    assert "morning_brief" in out and "end_of_day" in out and "running" in out.lower()


def test_auto_approve_is_reported_so_nobody_is_surprised(runner, seeded_db_path, capsys, monkeypatch):
    monkeypatch.setenv("PM_AUTO_APPROVE", "1")

    runner.main(["--db", str(seeded_db_path), "--print-schedule", "--gateway", "scripted"])

    out = capsys.readouterr().out
    assert "auto-approve: ON" in out and "log-only" in out


def test_the_publisher_in_use_is_reported(runner, seeded_db_path, capsys, monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "power_automate")
    monkeypatch.setenv("POWER_AUTOMATE_FLOW_URL", "http://127.0.0.1:9/nothing")

    runner.main(["--db", str(seeded_db_path), "--print-schedule", "--gateway", "scripted"])

    out = capsys.readouterr().out
    assert "REAL" in out and "Power Automate" in out
    assert "127.0.0.1" not in out  # the flow URL is a credential: never printed
