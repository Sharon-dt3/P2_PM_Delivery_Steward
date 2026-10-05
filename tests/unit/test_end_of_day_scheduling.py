"""PM-11: the end-of-day half of the schedule. It is a real job at the
configured end_of_day_time on working days only. What it does today is
capture and persist the end-of-day snapshot -- the evening half of the
morning-vs-evening comparison PM-22 will summarise. It calls no model and
publishes nothing; PM-22 adds the summary on top of this job.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger

from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.end_of_day_job import CAPTURED, SKIPPED_NON_WORKING_DAY, run_end_of_day_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.scheduling.scheduler import build_scheduler, is_due_for_end_of_day
from pm.seed.build import CHANNEL_ID
from pm.state.store import list_snapshot_timestamps, read_snapshot


def _config(**overrides):
    values = {
        "channel_id": CHANNEL_ID, "timezone": "Asia/Colombo", "working_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "morning_brief_time": time(8, 0), "end_of_day_time": time(17, 0),
    }
    return ProjectScheduleConfig(**{**values, **overrides})


WED_17_00_COLOMBO = datetime(2026, 9, 16, 11, 30, tzinfo=timezone.utc)


def test_is_due_at_the_configured_local_end_of_day_on_a_working_day():
    config = _config()

    assert is_due_for_end_of_day(config, WED_17_00_COLOMBO)
    assert not is_due_for_end_of_day(config, WED_17_00_COLOMBO + timedelta(minutes=1))
    assert not is_due_for_end_of_day(config, WED_17_00_COLOMBO.replace(hour=2, minute=30))  # the morning time


def test_not_due_on_a_weekend_or_a_non_working_date():
    assert not is_due_for_end_of_day(_config(), datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc))  # Saturday
    holiday = _config(non_working_dates=[date(2026, 9, 16)])
    assert not is_due_for_end_of_day(holiday, WED_17_00_COLOMBO)


def test_a_clock_override_run_captures_the_snapshot_at_the_simulated_time(seeded_db_path):
    result = run_end_of_day_job(_config(), moment=WED_17_00_COLOMBO, db_path=seeded_db_path)

    assert result.status == CAPTURED
    assert result.taken_at == WED_17_00_COLOMBO.isoformat()
    saved = read_snapshot(result.taken_at, db_path=seeded_db_path)
    assert saved.taken_at == result.taken_at and saved.timezone == "Asia/Colombo"


def test_a_repeat_run_for_the_same_moment_is_harmless(seeded_db_path):
    first = run_end_of_day_job(_config(), moment=WED_17_00_COLOMBO, db_path=seeded_db_path)
    second = run_end_of_day_job(_config(), moment=WED_17_00_COLOMBO, db_path=seeded_db_path)

    assert first.status == second.status == CAPTURED
    assert list_snapshot_timestamps(db_path=seeded_db_path).count(first.taken_at) == 1


def test_a_non_working_day_is_skipped_and_nothing_is_stored(seeded_db_path):
    saturday = datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc)

    result = run_end_of_day_job(_config(), moment=saturday, db_path=seeded_db_path)

    assert result.status == SKIPPED_NON_WORKING_DAY
    assert list_snapshot_timestamps(db_path=seeded_db_path) == []


def test_the_scheduler_registers_an_end_of_day_job_next_to_the_morning_one():
    scheduler = build_scheduler([_config(timezone="Asia/Tokyo", end_of_day_time=time(17, 30))], gateway=object())
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {f"pm:morning_brief:{CHANNEL_ID}", f"pm:end_of_day:{CHANNEL_ID}"}
    trigger: CronTrigger = jobs[f"pm:end_of_day:{CHANNEL_ID}"].trigger
    fields = {f.name: str(f) for f in trigger.fields}
    assert (fields["hour"], fields["minute"], fields["day_of_week"]) == ("17", "30", "mon,tue,wed,thu,fri")
    assert str(trigger.timezone) == "Asia/Tokyo"


def test_the_end_of_day_job_gets_no_model_and_the_morning_job_still_gets_one():
    gateway = ScriptedGateway()
    scheduler = build_scheduler([_config()], gateway=gateway)
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert "gateway" not in jobs[f"pm:end_of_day:{CHANNEL_ID}"].kwargs
    assert jobs[f"pm:morning_brief:{CHANNEL_ID}"].kwargs["gateway"] is gateway
    assert scheduler.running is False


def test_the_two_jobs_of_one_project_never_share_a_minute_unless_configured_to():
    config = _config()
    start = datetime(2026, 9, 14, tzinfo=timezone.utc)
    minutes = [start + timedelta(minutes=m) for m in range(5 * 24 * 60)]

    from pm.scheduling.scheduler import is_due_for_morning_brief

    assert [m for m in minutes if is_due_for_morning_brief(config, m) and is_due_for_end_of_day(config, m)] == []
