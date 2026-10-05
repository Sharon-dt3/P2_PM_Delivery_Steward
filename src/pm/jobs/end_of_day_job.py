"""PM-11: the job body the end-of-day cron fires, at the project's configured
end_of_day_time on its working days.

What it does today: capture and persist the end-of-day snapshot -- the evening
half of the morning-vs-evening comparison. It calls no model and sends nothing:
PM-22 (end-of-day summary) builds the diff and the summary on top of this job,
and sends go through the approval gate (PM-13). A job that fires on schedule and
does this one real, harmless thing is better than a cron wired to a function
that does not exist yet.

Same shape as run_morning_brief_job: `moment` defaults to now and a clock
override passes it explicitly; a non-working day is skipped before any work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from spine.config.calendar import is_working_day

from pm.jobs.snapshot_capture import capture_snapshot
from pm.mirror.hook import mirrored
from pm.scheduling.config import ProjectScheduleConfig
from pm.storage.db import DEFAULT_DB_PATH

SKIPPED_NON_WORKING_DAY = "skipped_non_working_day"
CAPTURED = "captured"


@dataclass(frozen=True)
class EndOfDayJobResult:
    channel_id: str
    taken_at: str
    status: str
    detail: str


@mirrored
def run_end_of_day_job(
    config: ProjectScheduleConfig,
    *,
    moment: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> EndOfDayJobResult:
    resolved_moment = moment or datetime.now(timezone.utc)
    local_day = resolved_moment.astimezone(ZoneInfo(config.timezone)).date()

    if not is_working_day(local_day, config):
        return EndOfDayJobResult(
            channel_id=config.channel_id,
            taken_at=resolved_moment.isoformat(),
            status=SKIPPED_NON_WORKING_DAY,
            detail="not a working day for this project",
        )

    snapshot = capture_snapshot(config, resolved_moment, db_path=db_path)
    return EndOfDayJobResult(
        channel_id=config.channel_id,
        taken_at=snapshot.taken_at,
        status=CAPTURED,
        detail="end-of-day snapshot captured",
    )
