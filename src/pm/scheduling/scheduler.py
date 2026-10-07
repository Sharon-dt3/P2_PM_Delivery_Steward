"""
PM-11: the clock, reused wholesale from spine (CHN-33's generalized
scheduling engine) rather than rebuilt -- "Scheduler already exists.
This is configuration, not construction," per this row's own tool
rationale. Mirrors p1.publishing.scheduler's own shape exactly: a thin
wrapper supplying _to_*_spec() (config -> ScheduleSpec) and this repo's
own job functions, with the CronTrigger wiring itself untouched, owned
entirely by spine.scheduling.scheduler.

Two jobs per project, each at its own configured local time on the
project's working days only: the morning brief (spine's build_scheduler)
and the end-of-day snapshot capture (added to the same scheduler with
spine's add_scheduled_jobs). The end-of-day job does not summarise yet --
that is PM-22 -- see pm.jobs.end_of_day_job.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from spine.scheduling.scheduler import ScheduleSpec, add_scheduled_jobs
from spine.scheduling.scheduler import build_scheduler as _spine_build_scheduler
from spine.scheduling.scheduler import is_due as _spine_is_due

from pm.jobs.end_of_day_job import run_end_of_day_job
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.storage.db import DEFAULT_DB_PATH

__all__ = ["build_scheduler", "is_due_for_end_of_day", "is_due_for_morning_brief"]


def _to_morning_brief_spec(config: ProjectScheduleConfig) -> ScheduleSpec:
    return ScheduleSpec(
        job_id=f"pm:morning_brief:{config.channel_id}",
        timezone=config.timezone,
        working_days=config.working_days,
        non_working_dates=config.non_working_dates,
        scheduled_time=config.morning_brief_time,
    )


def _to_end_of_day_spec(config: ProjectScheduleConfig) -> ScheduleSpec:
    return ScheduleSpec(
        job_id=f"pm:end_of_day:{config.channel_id}",
        timezone=config.timezone,
        working_days=config.working_days,
        non_working_dates=config.non_working_dates,
        scheduled_time=config.end_of_day_time,
    )


def is_due_for_end_of_day(config: ProjectScheduleConfig, moment: datetime) -> bool:
    """The same clock-override check as is_due_for_morning_brief(), against
    config's end_of_day_time."""
    return _spine_is_due(_to_end_of_day_spec(config), moment)


def is_due_for_morning_brief(config: ProjectScheduleConfig, moment: datetime) -> bool:
    """True iff `moment`, expressed in config's own local time, is
    exactly config's configured morning_brief_time on one of its
    configured working days (and not a non_working_date). Delegates to
    spine.scheduling.scheduler.is_due() -- the same clock-override
    mechanism P1's own is_due() uses. PM-11's own acceptance test,
    literal: "A clock-override run produces the brief at the simulated
    time.\""""
    return _spine_is_due(_to_morning_brief_spec(config), moment)


def build_scheduler(
    configs: list[ProjectScheduleConfig],
    gateway,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    publisher=None,
) -> BackgroundScheduler:
    """The real production scheduler: per project, one CronTrigger for the
    morning brief and one for the end-of-day snapshot, firing at that
    project's own local morning_brief_time / end_of_day_time on its own
    working days, in its own timezone. Callers add further jobs and start() the
    returned scheduler; nothing here starts it, so tests (and a demo
    inspecting the wiring) can build one without a real clock ever firing
    a job."""
    scheduler = _spine_build_scheduler(
        configs,
        to_spec=_to_morning_brief_spec,
        job_fn=run_morning_brief_job,
        job_kwargs=lambda config: {
            "config": config,
            "gateway": gateway,
            "db_path": db_path,
            "publisher": publisher,  # None: chosen from TEAMS_PUBLISHER_MODE when the job fires
        },
    )
    return add_scheduled_jobs(
        scheduler,
        configs,
        to_spec=_to_end_of_day_spec,
        job_fn=run_end_of_day_job,
        job_kwargs=lambda config: {"config": config, "gateway": gateway, "db_path": db_path, "publisher": publisher},
    )
