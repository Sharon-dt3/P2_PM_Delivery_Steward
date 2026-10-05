"""PM-11, audited: the schedule is per project timezone, so everything the
job derives from "today" must use the project's local date, and the clock
must agree with the real cron trigger. Colombo (UTC+5:30, the seeded
channel) never exposed a UTC-vs-local mix-up; teams far from UTC would.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import GENERATED, run_morning_brief_job
from pm.reporting.facts import compute_morning_brief_facts
from pm.scheduling.config import ProjectScheduleConfig
from pm.scheduling.scheduler import is_due_for_end_of_day, is_due_for_morning_brief
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import ChannelSnapshot, ProjectSnapshot

WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _config(tz, morning=time(8, 0), evening=time(17, 0)):
    return ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone=tz, working_days=WEEK,
        morning_brief_time=morning, end_of_day_time=evening,
    )


def _sprint_of(result):
    sprint = result.brief.facts.sprint
    return (sprint.sprint_id, sprint.day_number) if sprint else None


def test_a_monday_morning_brief_east_of_utc_is_that_mondays_sprint_day(seeded_db_path):
    # Mon 7 Sep 08:00 in Auckland is still Sun 6 Sep in UTC (sprint 12's last day).
    moment = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)
    config = _config("Pacific/Auckland")
    assert is_due_for_morning_brief(config, moment)

    result = run_morning_brief_job(config, ScriptedGateway(), moment=moment, db_path=seeded_db_path)

    assert result.status == GENERATED
    assert _sprint_of(result) == ("sprint-13", 1)


def test_an_evening_brief_west_of_utc_is_that_days_sprint_day(seeded_db_path):
    # Fri 4 Sep 17:00 in Los Angeles is already Sat 5 Sep in UTC.
    moment = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    config = _config("America/Los_Angeles", morning=time(17, 0))
    assert is_due_for_morning_brief(config, moment)

    result = run_morning_brief_job(config, ScriptedGateway(), moment=moment, db_path=seeded_db_path)

    assert _sprint_of(result) == ("sprint-12", 12)  # 24 Aug is day 1


def test_the_colombo_schedule_is_unchanged(seeded_db_path):
    moment = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # Wed 16 Sep 08:00 Colombo

    result = run_morning_brief_job(_config("Asia/Colombo"), ScriptedGateway(), moment=moment, db_path=seeded_db_path)

    assert _sprint_of(result) == ("sprint-13", 10)


def _facts_for(tz, taken_at):
    return compute_morning_brief_facts(ProjectSnapshot(
        taken_at=taken_at, items=[], commits=[], channel=ChannelSnapshot(channel_id="c", messages=[]),
        sprints=[], timezone=tz,
    ))


def test_a_snapshot_without_a_timezone_uses_utc_as_before():
    snapshot = ProjectSnapshot(taken_at="2026-09-16T23:59:59+00:00", items=[], commits=[],
                               channel=ChannelSnapshot(channel_id="c", messages=[]))

    assert snapshot.timezone == "UTC"


def test_the_local_date_is_what_the_facts_use():
    from pm.adapters.tracker import Sprint

    sprints = [Sprint(id="s1", display_name="S1", start_date="2026-09-07", end_date="2026-09-20")]
    snapshot = ProjectSnapshot(
        taken_at="2026-09-06T20:00:00+00:00", items=[], commits=[],
        channel=ChannelSnapshot(channel_id="c", messages=[]), sprints=sprints, timezone="Pacific/Auckland",
    )

    assert compute_morning_brief_facts(snapshot).sprint.day_number == 1
    utc = snapshot.model_copy(update={"timezone": "UTC"})
    assert compute_morning_brief_facts(utc).sprint is None  # 6 Sep UTC is before the sprint


def _fire_times(config_time, tz, start, end):
    trigger = CronTrigger(day_of_week="mon,tue,wed,thu,fri", hour=config_time.hour, minute=config_time.minute,
                          timezone=ZoneInfo(tz))
    fires, previous, now = [], None, start
    while True:
        nxt = trigger.get_next_fire_time(previous, now)
        if nxt is None or nxt >= end:
            return fires
        fires.append(nxt.astimezone(timezone.utc))
        previous, now = nxt, nxt + timedelta(seconds=1)


@pytest.mark.parametrize("tz", ["Asia/Colombo", "Pacific/Auckland", "America/Los_Angeles", "Europe/London"])
@pytest.mark.parametrize("which", ["morning", "end_of_day"])
def test_the_clock_override_agrees_with_the_real_cron_trigger(tz, which):
    """Every minute of a fortnight (it spans London's spring clock change):
    is_due says yes at exactly the instants the CronTrigger would fire."""
    config = _config(tz)
    is_due = is_due_for_morning_brief if which == "morning" else is_due_for_end_of_day
    at = config.morning_brief_time if which == "morning" else config.end_of_day_time
    start = datetime(2026, 3, 22, tzinfo=timezone.utc)
    end = start + timedelta(days=14)

    due = [start + timedelta(minutes=m) for m in range(14 * 24 * 60) if is_due(config, start + timedelta(minutes=m))]

    assert due == _fire_times(at, tz, start, end)
    assert len(due) == 10  # ten working days, one firing each
