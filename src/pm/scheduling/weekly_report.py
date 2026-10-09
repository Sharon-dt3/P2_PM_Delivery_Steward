"""The weekly status report on a schedule (PM-29): once a week, at the end of the working week, the report is made and offered as a proposal.

Opt-in (PM_WEEKLY_REPORT=1, or --weekly-report on the scheduler), so a scheduler started the old way is unchanged. When it is on, the report is made on
`PM_WEEKLY_REPORT_AT` (default "Fri 18:00", read in `PM_WEEKLY_REPORT_TZ`, default Asia/Colombo: 15 minutes after P1's Friday weekly digest). The scheduler
only creates the proposal. Nothing is sent: the proposal's card in Teams can only be rejected.

The job never raises into the scheduler. If the model that writes the narrative is unavailable the report is made without it, and that is logged; if the
report cannot be made at all the reason is logged and nothing is proposed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

from spine.scheduling.scheduler import ScheduleSpec, add_scheduled_jobs

from pm.jobs.weekly_report_job import WeeklyReport, run_weekly_report_job
from pm.storage.db import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

ENV_ENABLED = "PM_WEEKLY_REPORT"
ENV_AT = "PM_WEEKLY_REPORT_AT"
ENV_TZ = "PM_WEEKLY_REPORT_TZ"
DEFAULT_AT = "Fri 18:00"
DEFAULT_TZ = "Asia/Colombo"
JOB_ID = "pm:weekly_report"
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class WeeklySchedule:
    day: str
    at: time
    timezone: str


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "") == "1"


def weekly_schedule() -> WeeklySchedule:
    """The day, time and timezone the report is made, from the environment. A value that cannot be read is an error that says what is wrong."""
    raw = (os.environ.get(ENV_AT) or DEFAULT_AT).strip()
    day, _, clock = raw.partition(" ")
    day = day.strip().capitalize()[:3]
    try:
        hour, minute = (int(part) for part in clock.strip().split(":"))
        moment = time(hour, minute)
    except ValueError:
        raise ValueError(f"{ENV_AT} must look like 'Fri 18:00'; got {raw!r}") from None
    if day not in _DAYS:
        raise ValueError(f"{ENV_AT} must start with a weekday (Mon..Sun); got {raw!r}")
    return WeeklySchedule(day=day, at=moment, timezone=(os.environ.get(ENV_TZ) or DEFAULT_TZ).strip())


def run_weekly_report_scheduled(
    *, db_path: str | Path = DEFAULT_DB_PATH, timezone_name: str = DEFAULT_TZ, gateway=None, moment: datetime | None = None,
) -> WeeklyReport | None:
    """What the scheduler runs. Never raises."""
    try:
        try:
            return run_weekly_report_job(moment, db_path=db_path, timezone_name=timezone_name, gateway=gateway)
        except Exception as exc:  # the model is the part most likely to be down: the report is still worth making without its narrative
            if gateway is None:
                raise
            logger.warning("weekly_report_narrative_unavailable error=%s: %s", type(exc).__name__, exc)
            return run_weekly_report_job(moment, db_path=db_path, timezone_name=timezone_name, gateway=None)
    except Exception as exc:  # noqa: BLE001 - a scheduled job must not take the scheduler down
        logger.error("weekly_report_failed error=%s: %s", type(exc).__name__, exc)
        return None


def add_weekly_report_job(scheduler, *, db_path: str | Path = DEFAULT_DB_PATH, gateway=None) -> WeeklySchedule:
    """Add the weekly job to an existing scheduler. Returns the schedule, for the banner."""
    schedule = weekly_schedule()
    add_scheduled_jobs(
        scheduler, [schedule],
        to_spec=lambda s: ScheduleSpec(job_id=JOB_ID, timezone=s.timezone, working_days=[s.day], non_working_dates=[], scheduled_time=s.at),
        job_fn=run_weekly_report_scheduled,
        job_kwargs=lambda s: {"db_path": db_path, "timezone_name": s.timezone, "gateway": gateway},
    )
    return schedule
