"""PM-11: the job body the end-of-day cron fires, at the project's configured
end_of_day_time on its working days.

It captures and persists the end-of-day snapshot -- the evening half of the morning-vs-evening
comparison. Given a gateway (PM-22) it then builds the end-of-day summary from the DIFF between
that day's stored morning snapshot and this one (what shipped, what is still pending, what is newly
blocked, what else changed), has the model word it under the same grounding the brief gets, and
PROPOSES it: nothing is sent until the approval gate (PM-13) lets it through. With no gateway it
only captures the snapshot, as before. If no morning snapshot was stored for the day, the morning
state is rebuilt from the tracker's history (as-of, nothing saved) and the summary says so.

Same shape as run_morning_brief_job: `moment` defaults to now and a clock
override passes it explicitly; a non-working day is skipped before any work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from spine.config.calendar import is_working_day

from pm.adapters.tracker import TrackerMock
from pm.approval.proposals import NOT_ATTEMPTED, propose_end_of_day_summary
from pm.approval.service import ApprovalPolicy
from pm.jobs.proposal_flow import propose_for_approval
from pm.jobs.snapshot_capture import capture_snapshot
from pm.mirror.hook import mirrored
from pm.reporting.end_of_day_facts import compute_end_of_day_facts
from pm.reporting.end_of_day_summary import EndOfDaySummary, generate_end_of_day_summary
from pm.scheduling.config import ProjectScheduleConfig
from pm.state.diff import compute_delta
from pm.state.moments import parse_moment
from pm.state.snapshot import ProjectSnapshot, build_current_snapshot
from pm.state.store import list_snapshot_timestamps, read_snapshot
from pm.storage.db import DEFAULT_DB_PATH

SKIPPED_NON_WORKING_DAY = "skipped_non_working_day"
CAPTURED = "captured"
SUMMARISED = "summarised"


@dataclass(frozen=True)
class EndOfDayJobResult:
    channel_id: str
    taken_at: str
    status: str
    detail: str
    summary: EndOfDaySummary | None = None
    morning_source: str = ""  # stored | reconstructed
    delivery_status: str = NOT_ATTEMPTED  # proposed | already_proposed | failed | auto_sent | ... (see pm.approval.proposals)
    delivery_detail: str = ""
    proposal_id: str | None = None


def _morning_snapshot(config: ProjectScheduleConfig, evening: ProjectSnapshot, local_day: date, db_path) -> tuple[ProjectSnapshot, str]:
    """The day's morning snapshot: the earliest one stored earlier that same local day. If none was stored,
    the morning state rebuilt as of the configured morning time (nothing is saved), and said to be so."""
    zone = ZoneInfo(config.timezone)
    evening_moment = parse_moment(evening.taken_at)
    same_day = [
        t for t in list_snapshot_timestamps(db_path)
        if parse_moment(t) < evening_moment and parse_moment(t).astimezone(zone).date() == local_day
    ]
    if same_day:
        return read_snapshot(min(same_day, key=parse_moment), db_path=db_path), "stored"
    morning = datetime.combine(local_day, config.morning_brief_time, tzinfo=zone)
    return build_current_snapshot(db_path=db_path, taken_at=morning.isoformat(), tz_name=config.timezone), "reconstructed"


@mirrored
def run_end_of_day_job(
    config: ProjectScheduleConfig,
    gateway=None,
    *,
    moment: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    publisher=None,
    policy: ApprovalPolicy | None = None,
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
    if gateway is None:
        return EndOfDayJobResult(
            channel_id=config.channel_id, taken_at=snapshot.taken_at, status=CAPTURED, detail="end-of-day snapshot captured",
        )

    try:
        morning, source = _morning_snapshot(config, snapshot, local_day, db_path)
        delta = compute_delta(morning, snapshot, TrackerMock(db_path=db_path))
        facts = compute_end_of_day_facts(delta, morning, snapshot, morning_source=source)
        summary = generate_end_of_day_summary(facts, gateway)
    except Exception as exc:  # noqa: BLE001 - the snapshot is already stored; a failed summary must not kill the job
        return EndOfDayJobResult(
            channel_id=config.channel_id, taken_at=snapshot.taken_at, status=CAPTURED,
            detail=f"end-of-day snapshot captured; the summary failed: {type(exc).__name__}: {exc}",
        )
    status, detail, proposal_id = propose_for_approval(
        lambda: propose_end_of_day_summary(summary, config, local_date=local_day.isoformat(), taken_at=snapshot.taken_at, db_path=db_path),
        local_date=local_day.isoformat(), db_path=db_path, publisher=publisher, policy=policy,
    )
    return EndOfDayJobResult(
        channel_id=config.channel_id, taken_at=snapshot.taken_at, status=SUMMARISED,
        detail="end-of-day snapshot captured and summary generated from the diff", summary=summary, morning_source=source,
        delivery_status=status, delivery_detail=detail, proposal_id=proposal_id,
    )
