"""P2's catch-up on startup: the jobs that were due while the scheduler was stopped are made up when it starts (see spine.scheduling.catchup for the mechanism).

What P2 decides, because it is the only part that knows what its jobs do:

  what counts as done   A morning brief that was generated, an end-of-day summary that was summarised, a channel brief that was proposed from P1's record, a weekly
                        report that was made. A job that returned having had nothing to work from (an evening brief before P1 wrote its record, an end-of-day summary
                        whose model failed, a weekly report that could not be made) is not done: it is tried again at the next start.
  how a late run is held   A run more than six hours late is proposed for a person to approve and is never approved by the system, whatever PM_AUTO_APPROVE says:
                        a Friday evening summary posted on its own on Saturday would be news about a day that is over. The weekly report is never sent, so it needs no hold.
  how far back          PM_CATCH_UP_HOURS (default 72, so a stop on Friday afternoon is made up on Monday morning); 0 turns catch-up off. Only the latest missed run of
                        each job is made up.

The ledger is a table in the same database the jobs write to, so a scratch database (--db /tmp/x.db) is a scratch ledger.
"""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spine.scheduling.catchup import DEFAULT_GRACE, CatchUp, CatchUpItem, RunLedger

from pm.approval.service import load_approval_policy
from pm.jobs import channel_brief_job, end_of_day_job, morning_brief_job
from pm.scheduling import weekly_report as weekly

logger = logging.getLogger(__name__)

ENV_HOURS = "PM_CATCH_UP_HOURS"
DEFAULT_HOURS = 72

_DONE_STATUSES = {
    "pm:morning_brief:": {morning_brief_job.GENERATED, morning_brief_job.SKIPPED_NON_WORKING_DAY},
    "pm:end_of_day:": {end_of_day_job.SUMMARISED, end_of_day_job.SKIPPED_NON_WORKING_DAY},
    "pm:channel_": {channel_brief_job.PROPOSED_FROM_RECORD, channel_brief_job.SKIPPED_NON_WORKING_DAY},
}


def lookback() -> timedelta | None:
    """How far back a missed run is made up, from PM_CATCH_UP_HOURS; None when it is 0 (off). A value that cannot be read is an error that says what is wrong."""
    raw = (os.environ.get(ENV_HOURS) or str(DEFAULT_HOURS)).strip()
    try:
        hours = float(raw)
    except ValueError:
        raise ValueError(f"{ENV_HOURS} must be a number of hours (0 turns catch-up off); got {raw!r}") from None
    if hours < 0:
        raise ValueError(f"{ENV_HOURS} must not be negative; got {raw!r}")
    return timedelta(hours=hours) if hours else None


def succeeded(job_id: str, result: object) -> bool:
    """Whether a job's return value says it did its work."""
    if job_id == weekly.JOB_ID:
        return result is not None
    for prefix, statuses in _DONE_STATUSES.items():
        if job_id.startswith(prefix):
            return getattr(result, "status", None) in statuses
    return True


def hold(func) -> dict | None:
    """The keyword arguments that keep a late run from posting itself: the approval policy with auto-approve off. {} for the weekly report (never sent). None for
    a job that takes no policy, which is therefore not made up when late."""
    if func is weekly.run_weekly_report_scheduled:
        return {}
    if "policy" in inspect.signature(func).parameters:
        return {"policy": replace(load_approval_policy(), auto_approve=False)}
    return None


@dataclass
class CatchUpReport:
    enabled: bool
    armed_at: datetime | None = None
    just_armed: bool = False  # this start is the one that began the ledger: nothing earlier can be vouched for
    items: list[CatchUpItem] = field(default_factory=list)


_FROM_ENVIRONMENT = object()


def enable(scheduler, db_path: str | Path, *, now: datetime | None = None, window: timedelta | None | object = _FROM_ENVIRONMENT) -> CatchUpReport:
    """Record every run that finishes, and queue the runs that were missed. Call once, after every job is added and before the scheduler starts."""
    window = lookback() if window is _FROM_ENVIRONMENT else window
    if window is None:
        return CatchUpReport(enabled=False)
    now = now or datetime.now(timezone.utc)
    existed = _has_ledger(db_path)
    ledger = RunLedger(db_path, now=now)
    catch_up = CatchUp(scheduler, ledger, lookback=window, grace=DEFAULT_GRACE, succeeded=succeeded, hold=hold)
    catch_up.listen()
    items = catch_up.plan(now)
    catch_up.schedule(items)
    for item in items:
        logger.warning("catch_up job=%s scheduled_for=%s late=%s", item.job_id, item.scheduled_for.isoformat(), item.late)
    return CatchUpReport(enabled=True, armed_at=ledger.armed_at, just_armed=not existed, items=items)


def _has_ledger(db_path: str | Path) -> bool:
    import sqlite3

    path = Path(db_path)
    if not path.exists():
        return False
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scheduler_ledger'").fetchone() is not None
    finally:
        conn.close()
