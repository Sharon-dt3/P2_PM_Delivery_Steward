"""
PM-11: the actual job body a due project's cron fires -- see
pm.scheduling.scheduler for the clock. Builds and persists a real
ProjectSnapshot, computes MorningBriefFacts from it, and generates the
grounded brief -- exactly the pipeline scripts/run_morning_brief.py
already demonstrates by hand, wrapped here so a real scheduler (or a
clock-override demo) can drive it at the right moment instead of a human
running the script.

After generating the brief the job hands it to a Teams publisher chosen by
configuration (see pm.delivery.brief_delivery): log-only by default, so
nothing leaves the machine unless a real post is deliberately enabled and the
channel is allowlisted. That is not the approval gate -- PM-13 ("Approval gate
wired to the proposal spine") replaces the enabling switch with a proper
approval. A failed delivery is reported in the result, never raised.

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

from pm.adapters.teams import get_teams_publisher
from pm.delivery.brief_delivery import (
    FAILED,
    NOT_ATTEMPTED,
    DeliveryPolicy,
    DeliveryResult,
    deliver_brief,
    load_delivery_policy,
)
from pm.jobs.snapshot_capture import capture_snapshot
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import MorningBrief, generate_morning_brief
from pm.scheduling.config import ProjectScheduleConfig
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
    delivery_status: str = NOT_ATTEMPTED
    delivery_detail: str = ""


def run_morning_brief_job(
    config: ProjectScheduleConfig,
    gateway,
    *,
    moment: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    publisher=None,
    policy: DeliveryPolicy | None = None,
    redeliver: bool = False,
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
    snapshot = capture_snapshot(config, resolved_moment, db_path=db_path)

    facts = compute_morning_brief_facts(snapshot)
    brief = generate_morning_brief(facts, gateway)

    delivery = _deliver(config, brief, local_day.isoformat(), publisher, policy, db_path, redeliver)
    return MorningBriefJobResult(
        channel_id=config.channel_id,
        taken_at=taken_at,
        status=GENERATED,
        detail="morning brief generated and grounded",
        brief=brief,
        delivery_status=delivery.status,
        delivery_detail=delivery.detail,
    )


def _deliver(config, brief, local_date, publisher, policy, db_path, redeliver):
    """Hand the finished brief to the publisher. Resolving the publisher or
    the policy can itself fail (a real mode with no flow URL, say); that is a
    failed delivery, not a crashed job -- the brief is already made."""
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
        policy = policy if policy is not None else load_delivery_policy()
    except Exception as exc:  # noqa: BLE001 - a misconfigured publisher fails the delivery, not the job
        return DeliveryResult(FAILED, f"{type(exc).__name__}: {exc}")
    return deliver_brief(
        f"{config.message_label}Morning brief — {local_date}\n\n{brief.content}",
        kind="morning_brief",
        target_channel_id=config.publish_channel_id or config.channel_id,
        local_date=local_date,
        publisher=publisher,
        policy=policy,
        db_path=db_path,
        redeliver=redeliver,
    )
