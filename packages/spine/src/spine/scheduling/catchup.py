"""Catch-up on startup: what was due while the scheduler was not running.

APScheduler keeps its schedule in memory. A job whose fire time passes while the process is stopped is not late, it is forgotten: the 6-hour misfire
grace (see scheduler.add_scheduled_jobs) only covers a process that was running but delayed. So a runner stopped on Friday afternoon and started on Saturday
has silently skipped Friday. This module makes that visible and fixes it, for any cron job on any APScheduler scheduler.

  RunLedger   which scheduled runs finished, by job id and by the instant they were scheduled for. A small SQLite table; created on first use.
  CatchUp     at startup, for each cron job: find its most recent scheduled fire time inside the lookback window; if the ledger has no finished run for it,
              run it once now, passing `moment=<the scheduled time>` so it does the work of that day and not of today.

Four rules keep it safe:

  1. A ledger can only vouch for what it watched. The first time it is opened it records when it was "armed", and only fire times after that are ever caught
     up. Without this, the first start after installing would re-run this morning's brief, which already ran.
  2. Only the LATEST missed fire time of each job is made up, never every one: a morning brief for Thursday, made on Saturday, is noise.
  3. A run is recorded only when it finished without raising and the caller's own `succeeded(job_id, result)` agrees. A job that crashed, or that reports it
     had nothing to work from yet, is tried again at the next start.
  4. A run that is later than `grace` is "late": the caller's `hold(func)` decides how it is made safe (for a job that posts, to a person's approval and never
     automatically). A job that cannot be held is not caught up when late.

Nothing here knows what a job does. It reads APScheduler's own triggers and events.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.events import EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("spine.scheduling.catchup")

DEFAULT_LOOKBACK = timedelta(hours=72)
DEFAULT_GRACE = timedelta(hours=6)  # the same tolerance scheduler.add_scheduled_jobs gives a late fire
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS scheduler_runs (job_id TEXT NOT NULL, scheduled_for TEXT NOT NULL, ran_at TEXT NOT NULL, PRIMARY KEY (job_id, scheduled_for))",
    "CREATE TABLE IF NOT EXISTS scheduler_ledger (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a naive datetime has no instant: the scheduler works in timezone-aware times")
    return moment.astimezone(timezone.utc)


class RunLedger:
    """Which scheduled runs finished. `armed_at` is the first moment this ledger was opened: it only knows about what happened after."""

    def __init__(self, path: str | Path, *, now: datetime | None = None) -> None:
        self._path = str(path)
        with self._open() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO scheduler_ledger (key, value) VALUES ('armed_at', ?)", (_utc(now or datetime.now(timezone.utc)).isoformat(),))

    @contextmanager
    def _open(self):
        conn = sqlite3.connect(self._path, timeout=30)
        try:
            with conn:  # commits on success, rolls back on error
                yield conn
        finally:
            conn.close()

    @property
    def armed_at(self) -> datetime:
        with self._open() as conn:
            return datetime.fromisoformat(conn.execute("SELECT value FROM scheduler_ledger WHERE key = 'armed_at'").fetchone()[0])

    def done(self, job_id: str, scheduled_for: datetime) -> bool:
        with self._open() as conn:
            return conn.execute("SELECT 1 FROM scheduler_runs WHERE job_id = ? AND scheduled_for = ?", (job_id, _utc(scheduled_for).isoformat())).fetchone() is not None

    def record(self, job_id: str, scheduled_for: datetime, ran_at: datetime | None = None) -> None:
        with self._open() as conn:
            conn.execute("INSERT OR REPLACE INTO scheduler_runs (job_id, scheduled_for, ran_at) VALUES (?, ?, ?)",
                         (job_id, _utc(scheduled_for).isoformat(), _utc(ran_at or datetime.now(timezone.utc)).isoformat()))

    def runs(self, job_id: str) -> list[datetime]:
        with self._open() as conn:
            return [datetime.fromisoformat(row[0]) for row in conn.execute("SELECT scheduled_for FROM scheduler_runs WHERE job_id = ? ORDER BY scheduled_for", (job_id,))]


def latest_fire(trigger: CronTrigger, now: datetime, since: datetime) -> datetime | None:
    """The most recent time `trigger` was due in (since, now], or None. Fire times are the trigger's own: its timezone, its days, its minute."""
    now, since = _utc(now), _utc(since)
    fire = trigger.get_next_fire_time(None, since + timedelta(microseconds=1))
    latest = None
    while fire is not None and _utc(fire) <= now:
        latest = fire
        fire = trigger.get_next_fire_time(fire, fire)
    return _utc(latest) if latest is not None else None


@dataclass(frozen=True)
class CatchUpItem:
    job_id: str
    scheduled_for: datetime  # UTC
    late: bool  # later than the grace
    func: Callable
    kwargs: dict = field(default_factory=dict)


class CatchUp:
    """Wires a ledger to a scheduler: records every finished run, and makes up the ones that were missed.

    `succeeded(job_id, result) -> bool`: whether a job's return value means it did its work (default: it returned without raising).
    `hold(func) -> dict | None`: the extra keyword arguments that make a late run safe (to be proposed to a person, never posted by itself); {} when the job
    needs none (it never posts); None when it cannot be made safe, and a late run of it is then not made up."""

    def __init__(
        self, scheduler, ledger: RunLedger, *, lookback: timedelta = DEFAULT_LOOKBACK, grace: timedelta = DEFAULT_GRACE,
        succeeded: Callable[[str, object], bool] | None = None, hold: Callable[[Callable], dict | None] | None = None,
    ) -> None:
        self._scheduler, self._ledger = scheduler, ledger
        self._lookback, self._grace = lookback, grace
        self._succeeded = succeeded or (lambda job_id, result: True)
        self._hold = hold or (lambda func: None)
        self._made_up: dict[str, tuple[str, datetime]] = {}  # catch-up job id -> (the job it stands for, the fire time it makes up)

    def listen(self) -> None:
        """Record every run that finishes, live or made up. Call before the scheduler starts."""
        self._scheduler.add_listener(self._on_executed, EVENT_JOB_EXECUTED)

    def _on_executed(self, event: JobExecutionEvent) -> None:
        job_id, scheduled_for = self._made_up.get(event.job_id, (event.job_id, event.scheduled_run_time))
        try:
            if self._succeeded(job_id, event.retval):
                self._ledger.record(job_id, scheduled_for)
            else:
                logger.warning("catch_up_not_recorded job=%s scheduled_for=%s reason=the job reported it had not done its work", job_id, scheduled_for)
        except Exception as exc:  # noqa: BLE001 - a ledger problem must not take a job's thread down
            logger.error("catch_up_record_failed job=%s error=%s: %s", job_id, type(exc).__name__, exc)

    def plan(self, now: datetime | None = None) -> list[CatchUpItem]:
        """The runs to make up: for each cron job, its latest fire time since the ledger was armed and within the lookback, if no finished run is recorded for it."""
        now = _utc(now or datetime.now(timezone.utc))
        since = max(now - self._lookback, self._ledger.armed_at)
        items: list[CatchUpItem] = []
        for job in self._scheduler.get_jobs():
            if not isinstance(job.trigger, CronTrigger) or job.id in self._made_up:
                continue
            fire = latest_fire(job.trigger, now, since)
            if fire is None or self._ledger.done(job.id, fire):
                continue
            if "moment" not in inspect.signature(job.func).parameters:
                logger.warning("catch_up_skipped job=%s reason=the job cannot be told which moment to work for", job.id)
                continue
            late = now - fire > self._grace
            extra = self._hold(job.func) if late else {}
            if extra is None:
                logger.warning("catch_up_skipped job=%s scheduled_for=%s reason=it is late and cannot be held for a person's approval", job.id, fire.isoformat())
                continue
            items.append(CatchUpItem(job.id, fire, late, job.func, {**job.kwargs, **extra, "moment": fire}))
        return sorted(items, key=lambda item: item.scheduled_for)

    def schedule(self, items: list[CatchUpItem]) -> None:
        """Hand each run to the scheduler as a one-off job that starts now, on the scheduler's own threads (a slow model never holds up startup). The moment `plan`
        was asked about only decides what was missed; the run itself always starts at the real current time."""
        for item in items:
            once = f"{item.job_id}:catch-up:{item.scheduled_for.isoformat()}"
            self._made_up[once] = (item.job_id, item.scheduled_for)
            self._scheduler.add_job(item.func, trigger="date", run_date=datetime.now(timezone.utc), kwargs=item.kwargs, id=once, replace_existing=True,
                                    misfire_grace_time=3600)
