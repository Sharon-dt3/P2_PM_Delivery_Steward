"""Catch-up on startup: a runner stopped on Friday and started on Saturday makes up what Friday was due.

APScheduler forgets a fire time that passes while the process is stopped. These tests show what is made up, what is deliberately not, and that a run that is
hours late can never post itself.
"""

from __future__ import annotations

import importlib.util
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.events import EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import PENDING, ProposalStore
from spine.scheduling.catchup import CatchUp, RunLedger, latest_fire

from pm.jobs import channel_brief_job, end_of_day_job, morning_brief_job
from pm.scheduling import catch_up as p2
from pm.scheduling import weekly_report as weekly
from pm.scheduling.config import default_project_schedule_config
from pm.scheduling.scheduler import build_scheduler

COLOMBO = ZoneInfo("Asia/Colombo")
SAT_MORNING = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)  # Sat 08:30 in Colombo
ARMED = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)  # the ledger has been watching since Tuesday


def weekday_at(hour: int, minute: int = 0, days: str = "mon-fri") -> CronTrigger:
    return CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=COLOMBO)


def at_colombo(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=COLOMBO).astimezone(timezone.utc)


# --- when was a cron job last due ------------------------------------------------------------------------------------------------------------------


def test_the_latest_fire_is_the_triggers_own_last_time_in_its_own_timezone():
    # Saturday morning: a weekday 08:00 job last fired on Friday 08:00 Colombo, which is 02:30 UTC.
    assert latest_fire(weekday_at(8), SAT_MORNING, ARMED) == datetime(2026, 9, 18, 2, 30, tzinfo=timezone.utc)


def test_a_weekly_job_is_last_due_the_friday_before_and_never_on_a_weekday_it_does_not_run():
    friday_18 = weekday_at(18, days="fri")

    assert latest_fire(friday_18, datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc), ARMED) == at_colombo(18, 18)  # Monday morning: last Friday


def test_nothing_is_due_in_a_window_that_holds_no_fire_time():
    assert latest_fire(weekday_at(8), SAT_MORNING, at_colombo(18, 9)) is None  # armed after Friday's 08:00 and before any other fire


def test_a_fire_time_exactly_now_counts_and_one_a_moment_from_now_does_not():
    fire = at_colombo(18, 8)

    assert latest_fire(weekday_at(8), fire, ARMED) == fire
    assert latest_fire(weekday_at(8), fire - timedelta(seconds=1), ARMED) == at_colombo(17, 8)


# --- the ledger ----------------------------------------------------------------------------------------------------------------------------------------


def test_a_ledger_is_armed_once_and_remembers_runs_across_restarts(tmp_path):
    first = RunLedger(tmp_path / "ledger.db", now=ARMED)
    first.record("job", at_colombo(18, 8))
    second = RunLedger(tmp_path / "ledger.db", now=SAT_MORNING)  # a later start: the original moment stands

    assert second.armed_at == ARMED
    assert second.done("job", at_colombo(18, 8)) and not second.done("job", at_colombo(17, 8)) and not second.done("other", at_colombo(18, 8))
    assert second.runs("job") == [at_colombo(18, 8).astimezone(timezone.utc)]


def test_the_same_instant_in_another_timezone_is_the_same_run(tmp_path):
    ledger = RunLedger(tmp_path / "ledger.db", now=ARMED)
    ledger.record("job", datetime(2026, 9, 18, 2, 30, tzinfo=timezone.utc))

    assert ledger.done("job", datetime(2026, 9, 18, 8, 0, tzinfo=COLOMBO))


def test_a_time_with_no_timezone_is_refused_not_guessed(tmp_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        RunLedger(tmp_path / "ledger.db", now=ARMED).record("job", datetime(2026, 9, 18, 8, 0))  # noqa: DTZ001 - the point of the test


# --- what is planned ------------------------------------------------------------------------------------------------------------------------------------


def job(moment=None, **kwargs):  # a job that can be told which moment to work for
    return None


def job_without_moment():
    return None


def plan_for(tmp_path, *jobs, armed=ARMED, now=SAT_MORNING, lookback=timedelta(hours=72), hold=None, succeeded=None):
    scheduler = BackgroundScheduler()
    for job_id, func, trigger in jobs:
        scheduler.add_job(func, trigger=trigger, id=job_id, kwargs={"marker": job_id} if func is job else {})
    ledger = RunLedger(tmp_path / "ledger.db", now=armed)
    catch_up = CatchUp(scheduler, ledger, lookback=lookback, succeeded=succeeded, hold=hold or (lambda func: {}))
    return catch_up, ledger, catch_up.plan(now)


def test_a_job_that_was_due_and_never_ran_is_planned_for_its_scheduled_moment(tmp_path):
    _, _, items = plan_for(tmp_path, ("morning", job, weekday_at(8)))

    (item,) = items
    assert item.job_id == "morning" and item.scheduled_for == at_colombo(18, 8)
    assert item.kwargs == {"marker": "morning", "moment": at_colombo(18, 8)}  # the job works for Friday, not for today


def test_a_job_whose_run_is_recorded_is_not_planned(tmp_path):
    catch_up, ledger, _ = plan_for(tmp_path, ("morning", job, weekday_at(8)))
    ledger.record("morning", at_colombo(18, 8))

    assert catch_up.plan(SAT_MORNING) == []


def test_only_the_latest_missed_run_of_a_job_is_made_up(tmp_path):
    _, _, items = plan_for(tmp_path, ("morning", job, weekday_at(8)))  # Wed, Thu and Fri were all missed

    assert [item.scheduled_for for item in items] == [at_colombo(18, 8)]


def test_a_fire_before_the_ledger_was_armed_is_never_made_up(tmp_path):
    _, _, items = plan_for(tmp_path, ("morning", job, weekday_at(8)), armed=at_colombo(18, 9))  # armed after Friday 08:00: it cannot vouch for it

    assert items == []


def test_a_fire_older_than_the_lookback_is_not_made_up(tmp_path):
    _, _, items = plan_for(tmp_path, ("friday", job, weekday_at(18, days="fri")), now=datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc),
                           lookback=timedelta(hours=72))  # Wednesday: Friday 18:00 is 4 days back

    assert items == []


def test_a_job_that_cannot_be_told_which_moment_to_work_for_is_left_alone(tmp_path):
    _, _, items = plan_for(tmp_path, ("fixed", job_without_moment, weekday_at(8)))

    assert items == []


def test_a_job_that_is_not_on_a_cron_schedule_is_not_a_missed_run(tmp_path):
    scheduler = BackgroundScheduler()
    scheduler.add_job(job, trigger="interval", minutes=5, id="poll")
    catch_up = CatchUp(scheduler, RunLedger(tmp_path / "ledger.db", now=ARMED), hold=lambda func: {})

    assert catch_up.plan(SAT_MORNING) == []


def test_a_late_run_is_held_and_one_within_the_grace_is_not(tmp_path):
    held = []

    def hold(func):
        held.append(func)
        return {"policy": "held"}

    _, _, items = plan_for(tmp_path, ("morning", job, weekday_at(8)), now=at_colombo(18, 8) + timedelta(hours=1), hold=hold)
    assert [i.late for i in items] == [False] and "policy" not in items[0].kwargs and held == []  # an hour late: a normal run

    _, _, items = plan_for(tmp_path, ("morning", job, weekday_at(8)), hold=hold)
    assert [i.late for i in items] == [True] and items[0].kwargs["policy"] == "held" and held == [job]  # a day late: held


def test_a_late_run_that_cannot_be_held_is_not_made_up_at_all(tmp_path):
    _, _, items = plan_for(tmp_path, ("morning", job, weekday_at(8)), hold=lambda func: None)

    assert items == []


# --- what is recorded -----------------------------------------------------------------------------------------------------------------------------


def executed(job_id, scheduled_for, retval=None):
    return JobExecutionEvent(EVENT_JOB_EXECUTED, job_id, "default", scheduled_for, retval=retval)


def test_a_run_that_finished_is_recorded_under_the_moment_it_was_scheduled_for(tmp_path):
    catch_up, ledger, _ = plan_for(tmp_path, ("morning", job, weekday_at(8)))

    catch_up._on_executed(executed("morning", at_colombo(18, 8).astimezone(COLOMBO)))

    assert ledger.done("morning", at_colombo(18, 8))


def test_a_run_the_callers_check_says_did_not_do_its_work_is_not_recorded(tmp_path):
    catch_up, ledger, _ = plan_for(tmp_path, ("morning", job, weekday_at(8)), succeeded=lambda job_id, result: result == "ok")

    catch_up._on_executed(executed("morning", at_colombo(18, 8), retval="nothing to work from"))
    assert not ledger.done("morning", at_colombo(18, 8))

    catch_up._on_executed(executed("morning", at_colombo(18, 8), retval="ok"))
    assert ledger.done("morning", at_colombo(18, 8))


def test_a_made_up_run_is_recorded_for_the_fire_time_it_made_up_not_for_the_moment_it_ran(tmp_path):
    catch_up, ledger, items = plan_for(tmp_path, ("morning", job, weekday_at(8)))
    catch_up.schedule(items)

    catch_up._on_executed(executed(f"morning:catch-up:{items[0].scheduled_for.isoformat()}", SAT_MORNING))

    assert ledger.done("morning", at_colombo(18, 8)) and not ledger.done("morning", SAT_MORNING)
    assert catch_up.plan(SAT_MORNING) == []  # and so it is not planned again at the next start


# --- P2's decisions ------------------------------------------------------------------------------------------------------------------------------------


def test_the_lookback_is_72_hours_unless_the_environment_says_otherwise(monkeypatch):
    monkeypatch.delenv("PM_CATCH_UP_HOURS", raising=False)
    assert p2.lookback() == timedelta(hours=72)
    monkeypatch.setenv("PM_CATCH_UP_HOURS", "12")
    assert p2.lookback() == timedelta(hours=12)
    monkeypatch.setenv("PM_CATCH_UP_HOURS", "0")
    assert p2.lookback() is None


@pytest.mark.parametrize("raw", ["soon", "-5"])
def test_an_unreadable_lookback_is_an_error_that_says_what_is_wrong(monkeypatch, raw):
    monkeypatch.setenv("PM_CATCH_UP_HOURS", raw)

    with pytest.raises(ValueError, match="PM_CATCH_UP_HOURS"):
        p2.lookback()


class Result:
    def __init__(self, status):
        self.status = status


@pytest.mark.parametrize(("job_id", "status", "expected"), [
    ("pm:morning_brief:c", morning_brief_job.GENERATED, True),
    ("pm:morning_brief:c", morning_brief_job.SKIPPED_NON_WORKING_DAY, True),
    ("pm:end_of_day:c", end_of_day_job.SUMMARISED, True),
    ("pm:end_of_day:c", end_of_day_job.CAPTURED, False),  # the snapshot was taken but the summary failed: try again
    ("pm:channel_morning:c", channel_brief_job.PROPOSED_FROM_RECORD, True),
    ("pm:channel_evening:c", channel_brief_job.NO_DATA, False),  # P1's record was not written yet: try again
])
def test_which_results_count_as_the_job_having_done_its_work(job_id, status, expected):
    assert p2.succeeded(job_id, Result(status)) is expected


def test_a_weekly_report_that_could_not_be_made_is_not_done():
    assert p2.succeeded(weekly.JOB_ID, None) is False and p2.succeeded(weekly.JOB_ID, object()) is True


def test_a_late_run_is_held_even_when_auto_approve_is_on(monkeypatch):
    monkeypatch.setenv("PM_AUTO_APPROVE", "1")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")

    held = p2.hold(morning_brief_job.run_morning_brief_job)

    assert held["policy"].auto_approve is False and held["policy"].approver_ids == frozenset({"sharon.silva"})


def test_the_weekly_report_needs_no_hold_and_a_job_with_no_policy_cannot_be_held():
    assert p2.hold(weekly.run_weekly_report_scheduled) == {}
    assert p2.hold(job_without_moment) is None


# --- on the real P2 scheduler ---------------------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def runner():
    path = Path(__file__).parents[2] / "scripts" / "run_scheduler.py"
    spec = importlib.util.spec_from_file_location("run_scheduler_script_catch_up", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def auto_approve_is_on(monkeypatch):
    monkeypatch.setenv("PM_AUTO_APPROVE", "1")
    monkeypatch.setenv("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", "0")  # nothing but the hold stands between a late brief and the channel
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")


def wait_until(condition, seconds=40):
    deadline = _time.monotonic() + seconds
    while _time.monotonic() < deadline:
        if condition():
            return True
        _time.sleep(0.2)
    return False


def test_friday_is_made_up_on_saturday_held_for_a_person_and_recorded(runner, seeded_db_path, tmp_path, auto_approve_is_on):
    log = LogPublisher(tmp_path / "log.jsonl")
    RunLedger(seeded_db_path, now=ARMED)  # the scheduler has been watched since Tuesday
    scheduler = build_scheduler([default_project_schedule_config()], runner._ScriptedBoth(), db_path=seeded_db_path, publisher=log)

    report = p2.enable(scheduler, seeded_db_path, now=SAT_MORNING)

    assert not report.just_armed
    assert {item.job_id.split(":")[1] for item in report.items} == {"morning_brief", "end_of_day"}
    assert all(item.late for item in report.items) and {item.scheduled_for.date().isoformat() for item in report.items} == {"2026-09-18"}
    scheduler.start()
    try:
        ledger = RunLedger(seeded_db_path)
        assert wait_until(lambda: all(ledger.done(item.job_id, item.scheduled_for) for item in report.items)), "the made-up runs did not finish"
    finally:
        scheduler.shutdown(wait=True)

    store = ProposalStore(seeded_db_path)
    assert sorted(p.payload["local_date"] for p in store.list_by_status(PENDING)) == ["2026-09-18", "2026-09-18"]  # a brief and a summary, for Friday
    assert log.read_log() == []  # auto-approve is on, and still nothing was posted: a person has to approve

    again = build_scheduler([default_project_schedule_config()], runner._ScriptedBoth(), db_path=seeded_db_path, publisher=log)
    assert p2.enable(again, seeded_db_path, now=SAT_MORNING).items == []  # and the next start finds nothing missed


def test_a_run_that_is_only_a_little_late_is_made_up_as_a_normal_run(runner, seeded_db_path, tmp_path, auto_approve_is_on):
    RunLedger(seeded_db_path, now=ARMED)
    scheduler = build_scheduler([default_project_schedule_config()], runner._ScriptedBoth(), db_path=seeded_db_path, publisher=LogPublisher(tmp_path / "log.jsonl"))

    report = p2.enable(scheduler, seeded_db_path, now=at_colombo(18, 9))  # Friday 09:00: the brief was due at 08:00

    morning = next(item for item in report.items if "morning_brief" in item.job_id)
    assert not morning.late and "policy" not in morning.kwargs


def test_the_first_start_after_installing_makes_up_nothing_and_says_so(runner, seeded_db_path, tmp_path):
    scheduler = build_scheduler([default_project_schedule_config()], runner._ScriptedBoth(), db_path=seeded_db_path, publisher=LogPublisher(tmp_path / "log.jsonl"))

    report = p2.enable(scheduler, seeded_db_path, now=SAT_MORNING)  # no ledger yet: Friday's runs may well have happened

    assert report.enabled and report.just_armed and report.items == [] and report.armed_at == SAT_MORNING


def test_catch_up_can_be_turned_off_and_then_leaves_no_trace(runner, seeded_db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_CATCH_UP_HOURS", "0")
    scheduler = build_scheduler([default_project_schedule_config()], runner._ScriptedBoth(), db_path=seeded_db_path, publisher=LogPublisher(tmp_path / "log.jsonl"))

    report = p2.enable(scheduler, seeded_db_path, now=SAT_MORNING)

    assert not report.enabled and not p2._has_ledger(seeded_db_path)


def test_the_scheduler_script_says_what_it_made_up_and_no_catch_up_switches_it_off(runner, seeded_db_path, capsys, monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.delenv("PM_CHANNEL_BRIEFS", raising=False)
    monkeypatch.delenv("PM_WEEKLY_REPORT", raising=False)

    runner.main(["--db", str(seeded_db_path), "--gateway", "scripted", "--no-catch-up"], block=False)
    assert "Catch-up on startup: off." in capsys.readouterr().out and not p2._has_ledger(seeded_db_path)

    runner.main(["--db", str(seeded_db_path), "--gateway", "scripted"], block=False)
    assert "Catch-up on startup: armed now." in capsys.readouterr().out and p2._has_ledger(seeded_db_path)

    runner.main(["--db", str(seeded_db_path), "--gateway", "scripted"], block=False)
    assert "nothing was missed" in capsys.readouterr().out
