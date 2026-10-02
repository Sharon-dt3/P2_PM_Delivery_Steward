"""
PM-11: the actual job body a due project's cron fires -- see
pm.scheduling.scheduler for the clock. Builds and persists a real
ProjectSnapshot, computes MorningBriefFacts from it, and generates the
grounded brief -- exactly the pipeline scripts/run_morning_brief.py
already demonstrates by hand, wrapped here so a real scheduler (or a
clock-override demo) can drive it at the right moment instead of a human
running the script.

Deliberately does NOT publish anywhere yet. Every other send in this
programme goes through the proposal/approval spine (SPN-08/09, P1's own
CHN-18/CHN-22) -- PM-13 ("Approval gate wired to the proposal spine") is
explicitly the row that wires that gate onto this job's output.
Publishing without it here would be a real regression against that rule,
not a shortcut -- so this function's own job is done once it has
produced a grounded MorningBrief; what happens to that brief next is
PM-13's job, not this one's.

Mirrors p1.publishing.daily_job.run_daily_digest_job's own shape where
it applies: `moment` defaults to "now" and a demo's clock override
passes it explicitly (the same override mechanism
pm.scheduling.scheduler.is_due_for_morning_brief() uses); a
non-working-day call returns a clear skipped status rather than doing
any work, via spine.config.calendar.is_working_day() -- reused directly,
not reimplemented, since ProjectScheduleConfig already satisfies that
function's own WorkingCalendarConfig protocol (see
pm.scheduling.config's own docstring). It does not (yet) borrow
run_daily_digest_job's own proposal/idempotency machinery, since there
is nothing being sent yet for a proposal to govern -- only
save_snapshot()'s own DuplicateSnapshotError guard makes a repeat call
for the exact same simulated moment idempotent at the snapshot layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from spine.config.calendar import is_working_day

from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import MorningBrief, generate_morning_brief
from pm.scheduling.config import ProjectScheduleConfig
from pm.state.snapshot import build_current_snapshot
from pm.state.store import DuplicateSnapshotError, read_snapshot, save_snapshot
from pm.storage.db import DEFAULT_DB_PATH

SKIPPED_NON_WORKING_DAY = "skipped_non_working_day"
GENERATED = "generated"


@dataclass(frozen=True)
class MorningBriefJobResult:
    channel_id: str
    taken_at: str
    status: str
    detail: str
    brief: MorningBrief | None = None


def run_morning_brief_job(
    config: ProjectScheduleConfig,
    gateway,
    *,
    moment: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> MorningBriefJobResult:
    """Idempotent for a given `moment`: calling this twice for the exact
    same simulated (or, in production, real) instant reads back the
    snapshot the first call already persisted rather than persisting a
    second, redundant one -- see save_snapshot()'s own
    DuplicateSnapshotError. `moment` defaults to "now" (UTC); a
    clock-override demo passes it explicitly, the same override
    pm.scheduling.scheduler.is_due_for_morning_brief() itself uses --
    PM-11's own acceptance test, literal: "A clock-override run produces
    the brief at the simulated time.\""""
    resolved_moment = moment or datetime.now(timezone.utc)
    local_day = resolved_moment.astimezone(ZoneInfo(config.timezone)).date()

    if not is_working_day(local_day, config):
        return MorningBriefJobResult(
            channel_id=config.channel_id,
            taken_at=resolved_moment.isoformat(),
            status=SKIPPED_NON_WORKING_DAY,
            detail="not a working day for this project",
        )

    taken_at = resolved_moment.isoformat()
    snapshot = build_current_snapshot(db_path=db_path, taken_at=taken_at)
    try:
        save_snapshot(snapshot, db_path=db_path)
    except DuplicateSnapshotError:
        # This exact moment was already run once -- read back what that
        # run persisted rather than silently discarding it or crashing a
        # second, harmless call for the same instant.
        snapshot = read_snapshot(taken_at, db_path=db_path)

    facts = compute_morning_brief_facts(snapshot)
    brief = generate_morning_brief(facts, gateway)

    return MorningBriefJobResult(
        channel_id=config.channel_id,
        taken_at=taken_at,
        status=GENERATED,
        detail="morning brief generated and grounded",
        brief=brief,
    )
