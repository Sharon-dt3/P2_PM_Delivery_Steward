"""
Project schedule configuration (PM-11).

The three plain facts spine.scheduling.scheduler.ScheduleSpec needs from
a calendar -- timezone, working_days, non_working_dates -- are never
re-typed here as a second, competing copy of P1's own
config/channels/proj-gamma.yaml: default_project_schedule_config() reads
them straight out of P1's real ChannelConfigStore, the same reuse
posture pm.adapters.teams.get_teams_reader() already takes toward P1's
fixture data. If proj-gamma.yaml's timezone or working days ever change,
this repo's own schedule follows without anyone having to remember to
update a second file.

morning_brief_time/end_of_day_time have no P1 analog -- ChannelConfig's
own daily_digest_time/weekly_digest_time govern P1's own digest job, not
either of this repo's two jobs -- so they are this repo's own new
fields, given sensible defaults below rather than pulled from anywhere
else.

end_of_day_time is validated configuration like the morning time, and
pm.scheduling.scheduler.build_scheduler() registers a real job against it:
pm.jobs.end_of_day_job captures the end-of-day snapshot. PM-22 (end-of-day
summary) builds the summary on top of that job.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

from pydantic import BaseModel

from pm.adapters.teams import P1_REPO_ROOT
from pm.seed.build import CHANNEL_ID

P1_CHANNEL_CONFIG_DIR = P1_REPO_ROOT / "config" / "channels"

# This repo's own two job times -- no P1 analog to reuse (see module
# docstring). Chosen to bracket a normal working day: morning brief
# before standup, end-of-day summary after most work has landed.
DEFAULT_MORNING_BRIEF_TIME = time(8, 0)
DEFAULT_END_OF_DAY_TIME = time(17, 0)


class ProjectScheduleConfig(BaseModel):
    """Satisfies spine.config.calendar.WorkingCalendarConfig
    structurally (working_days/non_working_dates fields present, same
    names) -- pm.jobs.morning_brief_job reuses spine's is_working_day()
    directly against an instance of this class with zero adapter code,
    the same structural-Protocol reuse CHN-33 generalized calendar.py
    for in the first place."""

    channel_id: str
    timezone: str
    working_days: list[str]
    non_working_dates: list[date] = []
    morning_brief_time: time
    end_of_day_time: time


def default_project_schedule_config(
    *,
    channel_id: str = CHANNEL_ID,
    config_dir: str | Path = P1_CHANNEL_CONFIG_DIR,
    morning_brief_time: time = DEFAULT_MORNING_BRIEF_TIME,
    end_of_day_time: time = DEFAULT_END_OF_DAY_TIME,
) -> ProjectScheduleConfig:
    """channel_id/config_dir default to this repo's own seeded channel
    (pm.seed.build.CHANNEL_ID) and P1's real config/channels directory --
    overridable so a test can point at a channel P1 has no config file
    for at all, without this function ever reaching onto disk."""
    from p1.config.loader import ChannelConfigStore

    p1_config = ChannelConfigStore(config_dir).get_channel_config(channel_id)
    return ProjectScheduleConfig(
        channel_id=channel_id,
        timezone=p1_config.timezone,
        working_days=p1_config.working_days,
        non_working_dates=p1_config.non_working_dates,
        morning_brief_time=morning_brief_time,
        end_of_day_time=end_of_day_time,
    )
