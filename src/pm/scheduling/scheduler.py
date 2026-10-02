"""
PM-11: the clock, reused wholesale from spine (CHN-33's generalized
scheduling engine) rather than rebuilt -- "Scheduler already exists.
This is configuration, not construction," per this row's own tool
rationale. Mirrors p1.publishing.scheduler's own shape exactly: a thin
wrapper supplying _to_morning_brief_spec() (config -> ScheduleSpec) and
this repo's own job function, with the CronTrigger wiring itself
untouched, owned entirely by spine.scheduling.scheduler.

Only the morning brief job is registered by build_scheduler() below.
ProjectScheduleConfig.end_of_day_time is real, validated configuration --
but there is no end-of-day job function to schedule it against yet
(PM-22 builds that). See pm.scheduling.config's own docstring for why
that is a deliberate gap, not an oversight; tests/unit/test_scheduling.py
asserts directly that no job is registered for it, so that gap stays
visible rather than silently forgotten once PM-22 does land.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from spine.scheduling.scheduler import ScheduleSpec
from spine.scheduling.scheduler import build_scheduler as _spine_build_scheduler
from spine.scheduling.scheduler import is_due as _spine_is_due

from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.storage.db import DEFAULT_DB_PATH

__all__ = ["is_due_for_morning_brief", "build_scheduler"]


def _to_morning_brief_spec(config: ProjectScheduleConfig) -> ScheduleSpec:
    return ScheduleSpec(
        job_id=f"pm:morning_brief:{config.channel_id}",
        timezone=config.timezone,
        working_days=config.working_days,
        non_working_dates=config.non_working_dates,
        scheduled_time=config.morning_brief_time,
    )


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
) -> BackgroundScheduler:
    """The real production scheduler: one CronTrigger per project, firing
    at that project's own local morning_brief_time on its own working
    days, in its own timezone. Callers add further jobs and start() the
    returned scheduler; nothing here starts it, so tests (and a demo
    inspecting the wiring) can build one without a real clock ever firing
    a job."""
    return _spine_build_scheduler(
        configs,
        to_spec=_to_morning_brief_spec,
        job_fn=run_morning_brief_job,
        job_kwargs=lambda config: {
            "config": config,
            "gateway": gateway,
            "db_path": db_path,
        },
    )
