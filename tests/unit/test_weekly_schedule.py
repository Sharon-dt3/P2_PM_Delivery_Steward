"""The weekly status report on a schedule: opt-in, once a week at the end of the working week, never raising into the scheduler.

It only creates the proposal (nothing is sent), and a model that is down costs the report its narrative, not its existence.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from spine.approval.proposals import ProposalStore

from pm.reporting.scripted_weekly import ScriptedWeeklyGateway
from pm.scheduling import weekly_report as weekly
from pm.scheduling.scheduler import build_scheduler

FRIDAY_EVENING = datetime(2026, 9, 18, 12, 30, tzinfo=timezone.utc)  # 18:00 Friday in Colombo


@pytest.fixture()
def runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_scheduler.py"
    spec = importlib.util.spec_from_file_location("run_scheduler_weekly_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (weekly.ENV_ENABLED, weekly.ENV_AT, weekly.ENV_TZ):
        monkeypatch.delenv(name, raising=False)


# --- when ----------------------------------------------------------------------------------------------------------------------------------


def test_the_default_is_friday_at_six_in_the_evening_in_colombo_and_it_is_off_unless_asked_for():
    assert weekly.weekly_schedule() == weekly.WeeklySchedule(day="Fri", at=time(18, 0), timezone="Asia/Colombo")
    assert weekly.enabled() is False


def test_it_is_on_only_when_the_setting_is_exactly_one(monkeypatch):
    monkeypatch.setenv(weekly.ENV_ENABLED, "1")
    assert weekly.enabled() is True
    for other in ("0", "true", "yes", ""):
        monkeypatch.setenv(weekly.ENV_ENABLED, other)
        assert weekly.enabled() is False


def test_the_day_time_and_timezone_are_settings(monkeypatch):
    monkeypatch.setenv(weekly.ENV_AT, "thu 16:30")
    monkeypatch.setenv(weekly.ENV_TZ, "America/New_York")

    assert weekly.weekly_schedule() == weekly.WeeklySchedule(day="Thu", at=time(16, 30), timezone="America/New_York")


@pytest.mark.parametrize("bad", ["Friday", "18:00", "Fun 18:00", "Fri 25:00", "Fri 18", "Fri 18:00:00"])
def test_a_setting_that_cannot_be_read_says_what_is_wrong(monkeypatch, bad):
    monkeypatch.setenv(weekly.ENV_AT, bad)

    with pytest.raises(ValueError, match=weekly.ENV_AT):
        weekly.weekly_schedule()


def test_the_job_fires_on_fridays_at_that_time_in_that_timezone_and_on_no_other_day(seeded_db_path):
    scheduler = build_scheduler([], ScriptedWeeklyGateway(), db_path=seeded_db_path)

    schedule = weekly.add_weekly_report_job(scheduler, db_path=seeded_db_path, gateway=None)
    job = scheduler.get_job(weekly.JOB_ID)
    after_a_friday = datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)  # 18:30 Friday in Colombo: this Friday's run has passed
    nxt = job.trigger.get_next_fire_time(None, after_a_friday)

    assert schedule.day == "Fri"
    assert (nxt.weekday(), nxt.hour, nxt.minute, str(nxt.tzinfo)) == (4, 18, 0, "Asia/Colombo") and nxt.date().isoformat() == "2026-09-25"
    assert job.trigger.get_next_fire_time(None, datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)).weekday() == 4  # from Saturday: the next Friday


# --- what it does --------------------------------------------------------------------------------------------------------------------------


def test_the_scheduled_run_makes_the_report_and_only_proposes_it(seeded_db_path):
    report = weekly.run_weekly_report_scheduled(db_path=seeded_db_path, gateway=ScriptedWeeklyGateway(), moment=FRIDAY_EVENING)

    proposal = ProposalStore(seeded_db_path).get(report.proposal_id)
    assert report.created and proposal.status == "pending" and proposal.type == "weekly_status_report"
    assert "In plain language:" in proposal.payload["content"]


def test_a_second_run_in_the_same_week_finds_the_first_report_and_proposes_nothing_new(seeded_db_path):
    first = weekly.run_weekly_report_scheduled(db_path=seeded_db_path, gateway=None, moment=FRIDAY_EVENING)
    again = weekly.run_weekly_report_scheduled(db_path=seeded_db_path, gateway=None, moment=FRIDAY_EVENING)

    assert first.created and not again.created and first.proposal_id == again.proposal_id


def test_a_model_that_is_down_costs_the_report_its_narrative_and_nothing_else(seeded_db_path, caplog):
    class Down:
        def generate(self, prompt, **kwargs):
            raise ConnectionError("the model is unreachable")

    report = weekly.run_weekly_report_scheduled(db_path=seeded_db_path, gateway=Down(), moment=FRIDAY_EVENING)

    assert report is not None and report.created and "In plain language" not in report.text and report.problems == []
    assert "weekly_report_narrative_unavailable" in caplog.text


def test_a_run_that_cannot_make_the_report_at_all_says_why_and_never_raises(tmp_path, caplog):
    report = weekly.run_weekly_report_scheduled(db_path=tmp_path / "no_such.db", gateway=None, moment=FRIDAY_EVENING)

    assert report is None and "weekly_report_failed" in caplog.text


# --- the scheduler script -----------------------------------------------------------------------------------------------------------------------


def test_the_scheduler_script_adds_the_weekly_job_only_when_asked_and_prints_it(runner, seeded_db_path, capsys):
    runner.main(["--print-schedule", "--db", str(seeded_db_path), "--gateway", "scripted"])
    off = capsys.readouterr().out
    runner.main(["--print-schedule", "--db", str(seeded_db_path), "--gateway", "scripted", "--weekly-report", "--weekly-gateway", "scripted"])
    on = capsys.readouterr().out

    assert "weekly_report" not in off
    assert "weekly_report    at Fri 18:00 Asia/Colombo" in on and "a draft proposal; never sent" in on


def test_the_scheduler_script_can_run_the_weekly_job_once_at_a_chosen_moment(runner, seeded_db_path, capsys):
    status = runner.main(["--once", "--job", "weekly", "--at", "2026-09-18T18:00", "--db", str(seeded_db_path), "--weekly-gateway", "scripted"])

    assert status == 0 and "weekly report: proposed " in capsys.readouterr().out
    assert [p.type for p in ProposalStore(seeded_db_path).list_by_status("pending")] == ["weekly_status_report"]
