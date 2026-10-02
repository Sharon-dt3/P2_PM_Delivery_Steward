"""
The clock, split from the job -- generalized (CHN-33 extraction of
P1's publishing/scheduler.py, CHN-17's own original framing).

"A button labelled 'run the digest' is a test harness, not a
scheduler." Everything this module decides -- is a given schedule due
right now -- is expressed as one pure function, is_due(spec, moment),
that takes the moment as an ordinary argument rather than reading a
clock itself. That is the entire "clock override for demos" mechanism:
a demo calls is_due(spec, moment) with whatever moment it wants to
pretend it is, and gets exactly the same decision production would
make at that real moment -- no fake clock, no monkeypatched datetime,
no APScheduler internals to fight.

build_scheduler() is the real production wiring: one real APScheduler
CronTrigger per schedule, firing at that schedule's own configured
local time, on its own configured working days, in its own timezone.
APScheduler (not this module) is what tracks wall-clock time in
production; build_scheduler() only translates a ScheduleSpec into the
CronTrigger fields that make APScheduler fire at the same moments
is_due() would say yes to.

GENERALIZED FROM P1's ORIGINAL (which took a concrete ChannelConfig and
hardcoded a call to run_daily_digest_job): this version knows nothing
about digests, channels, or Teams. It takes a ScheduleSpec (five plain
fields any agent's config can produce) and any job_fn/job_kwargs pair.
P1's own publishing/scheduler.py is now a thin wrapper: it defines how
to turn a ChannelConfig into a ScheduleSpec and how to build
run_daily_digest_job's kwargs, and calls this module's build_scheduler
with those two functions. A future agent with its own config shape and
its own job function does the same, without touching this file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TypeVar

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# CronTrigger's day_of_week wants APScheduler's own abbreviations, which
# happen to be the same three letters lower-cased as the "Mon".."Sun"
# convention most config schemas in this programme use -- spelled out
# explicitly here rather than relying on that coincidence continuing to
# hold for every future agent's own day-name spelling.
_CRON_DAY_NAMES = {
    "Mon": "mon", "Tue": "tue", "Wed": "wed", "Thu": "thu",
    "Fri": "fri", "Sat": "sat", "Sun": "sun",
}

T = TypeVar("T")


@dataclass(frozen=True)
class ScheduleSpec:
    """The five plain facts build_scheduler()/is_due() actually need
    from a config, independent of what that config is otherwise for.
    `job_id` is the caller's own choice of a stable, unique APScheduler
    job id (P1's wrapper uses f"daily_digest:{channel_id}", say) --
    this module never invents one on the caller's behalf."""

    job_id: str
    timezone: str
    working_days: list[str]
    non_working_dates: list[date]
    scheduled_time: time


def is_due(spec: ScheduleSpec, moment: datetime) -> bool:
    """True iff `moment`, expressed in spec's own local time, is
    exactly spec's configured scheduled_time on one of its configured
    working days (and not a non_working_date). Minute resolution, not
    second/microsecond -- a scheduler firing once a minute (real
    APScheduler, or a demo stepping through a day minute-by-minute)
    sees exactly one due minute per spec per day, never zero and never
    more than one, since scheduled_time is a single hour:minute pair.

    `moment` may be any timezone-aware datetime (UTC in production, or
    whatever a demo chooses) -- it is converted to spec.timezone before
    anything is compared, since "spec-local time" is the only time this
    function knows about."""
    from zoneinfo import ZoneInfo

    local = moment.astimezone(ZoneInfo(spec.timezone))
    if local.date() in spec.non_working_dates:
        return False
    weekday_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    if weekday_names[local.weekday()] not in spec.working_days:
        return False
    return local.hour == spec.scheduled_time.hour and local.minute == spec.scheduled_time.minute


def add_scheduled_jobs(
    scheduler: BackgroundScheduler,
    configs: list[T],
    to_spec: Callable[[T], ScheduleSpec],
    job_fn: Callable[..., None],
    job_kwargs: Callable[[T], dict],
) -> BackgroundScheduler:
    """Adds one CronTrigger job per config to an EXISTING scheduler --
    the job-wiring half of build_scheduler(), split out so a caller that
    already owns a scheduler (e.g. a live runner that also has ingest and
    nudge jobs) can add a second kind of schedule (weekly, say) without
    duplicating any of this. Behaviour is exactly what build_scheduler()
    always did per config; returns the same scheduler for chaining."""
    from zoneinfo import ZoneInfo

    for config in configs:
        spec = to_spec(config)
        day_of_week = ",".join(
            _CRON_DAY_NAMES[day] for day in spec.working_days if day in _CRON_DAY_NAMES
        )
        trigger = CronTrigger(
            day_of_week=day_of_week,
            hour=spec.scheduled_time.hour,
            minute=spec.scheduled_time.minute,
            timezone=ZoneInfo(spec.timezone),
        )
        scheduler.add_job(
            job_fn,
            trigger=trigger,
            id=spec.job_id,
            kwargs=job_kwargs(config),
            # APScheduler's own default is 1 second -- a laptop sleeping or a
            # brief network stall past the exact fire time silently skips the
            # whole day's job rather than running it late (see DECISION_LOG.md,
            # 2026-09-23, the missed p1-agent-test digest). 6h tolerates a
            # laptop being asleep overnight-ish without giving up entirely.
            misfire_grace_time=6 * 3600,
        )
    return scheduler


def build_scheduler(
    configs: list[T],
    to_spec: Callable[[T], ScheduleSpec],
    job_fn: Callable[..., None],
    job_kwargs: Callable[[T], dict],
) -> BackgroundScheduler:
    """The real production scheduler: one CronTrigger per config,
    firing at that config's own local scheduled_time on its own working
    days, in its own timezone -- so several schedules in several
    timezones each fire at their own correct wall-clock moment without
    this process ever converting anything to a shared reference time
    itself.

    `to_spec` turns one of the caller's own config objects into a
    ScheduleSpec (the only part of this function that knows anything
    about the caller's config shape). `job_kwargs` builds that config's
    own kwargs dict for job_fn -- also entirely the caller's concern;
    this module never inspects job_fn's signature.

    Callers add further jobs and start() the returned scheduler;
    nothing here starts it, so tests can inspect the wiring without
    ever running a real background thread."""
    return add_scheduled_jobs(BackgroundScheduler(), configs, to_spec, job_fn, job_kwargs)
