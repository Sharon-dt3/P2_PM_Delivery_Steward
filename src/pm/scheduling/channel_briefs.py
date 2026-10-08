"""When the channel briefs run: per real channel, in the channel's own timezone and on its own working days (from P1's channel config).

  morning   08:00, a brief of what the team said on the last working day
  evening   P1's daily digest time + EVENING_AFTER_DIGEST_MINUTES: P1 writes the day's outcome record after its digest, so the evening
            summary of the day waits for it (a channel whose digest is 17:30 gets 17:45), never reads a record that is not written yet

Channels are named (P1's display_name, e.g. "p1-agent-test") or given by id, and must be on P1's allowlist.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

from spine.scheduling.scheduler import ScheduleSpec, add_scheduled_jobs

from pm.channelbrief.facts import EVENING, MORNING
from pm.jobs.channel_brief_job import run_channel_brief_job
from pm.scheduling.config import (
    P1_CHANNEL_CONFIG_DIR,
    ProjectScheduleConfig,
    default_project_schedule_config,
)
from pm.storage.db import DEFAULT_DB_PATH

MORNING_AT = time(8, 0)
EVENING_AFTER_DIGEST_MINUTES = 15


class UnknownChannel(ValueError):
    """A channel name or id that is not on P1's allowlist."""


def resolve_channel(name_or_id: str, config_dir: str | Path = P1_CHANNEL_CONFIG_DIR) -> tuple[str, str]:
    """(channel id, display name) for a channel given by P1 display name (any case) or id. Raises UnknownChannel for anything not allowlisted."""
    from p1.config.loader import ChannelConfigStore

    store = ChannelConfigStore(config_dir)
    wanted = name_or_id.strip().lower()
    for channel_id in store.list_allowlisted_channels():
        cfg = store.get_channel_config(channel_id)
        if wanted in (channel_id.lower(), cfg.display_name.strip().lower()):
            return channel_id, cfg.display_name
    raise UnknownChannel(f"{name_or_id!r} is not an allowlisted channel in {config_dir}")


def channel_schedule_config(channel_id: str, config_dir: str | Path = P1_CHANNEL_CONFIG_DIR) -> ProjectScheduleConfig:
    from p1.config.loader import ChannelConfigStore

    digest = ChannelConfigStore(config_dir).get_channel_config(channel_id).daily_digest_time
    evening = (datetime.combine(date(2000, 1, 1), digest) + timedelta(minutes=EVENING_AFTER_DIGEST_MINUTES)).time().replace(second=0, microsecond=0)
    return default_project_schedule_config(channel_id=channel_id, config_dir=config_dir, morning_brief_time=MORNING_AT, end_of_day_time=evening)


def _spec(kind: str):
    def to_spec(entry: tuple[ProjectScheduleConfig, str]) -> ScheduleSpec:
        config, _name = entry
        return ScheduleSpec(
            job_id=f"pm:channel_{kind}:{config.channel_id}", timezone=config.timezone, working_days=config.working_days,
            non_working_dates=config.non_working_dates, scheduled_time=config.morning_brief_time if kind == MORNING else config.end_of_day_time,
        )

    return to_spec


def add_channel_brief_jobs(scheduler, channels: list[str], *, db_path: str | Path = DEFAULT_DB_PATH, publisher=None,
                           config_dir: str | Path = P1_CHANNEL_CONFIG_DIR):
    """Add a morning and an evening job for each named channel to an existing scheduler. Returns [(config, display name)] for the banner."""
    entries: list[tuple[ProjectScheduleConfig, str]] = []
    for name in channels:
        channel_id, display = resolve_channel(name, config_dir)
        entries.append((channel_schedule_config(channel_id, config_dir), display))
    for kind in (MORNING, EVENING):
        add_scheduled_jobs(
            scheduler, entries, to_spec=_spec(kind), job_fn=run_channel_brief_job,
            job_kwargs=lambda entry, kind=kind: {"config": entry[0], "channel_name": entry[1], "kind": kind, "db_path": db_path, "publisher": publisher},
        )
    return entries
