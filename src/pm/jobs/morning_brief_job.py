"""
PM-11: the actual job body a due project's cron fires -- see
pm.scheduling.scheduler for the clock. Builds and persists a real
ProjectSnapshot, computes MorningBriefFacts from it, and generates the
grounded brief -- exactly the pipeline scripts/run_morning_brief.py
already demonstrates by hand, wrapped here so a real scheduler (or a
clock-override demo) can drive it at the right moment instead of a human
running the script.

After generating the brief the job PROPOSES it (pm.approval.proposals): one
pending proposal per target channel per local day, and nothing is sent. A person
approves, rejects or edits-then-approves it through pm.approval.service (a Teams
card or scripts/approve.py), and only then is it posted -- through the publisher
chosen by TEAMS_PUBLISHER_MODE, logged and audited. PM-13 is that gate.

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
from pm.approval.proposals import (
    ALREADY_PROPOSED,
    AUTO_SEND_FAILED,
    AUTO_SENT,
    FAILED,
    NOT_ATTEMPTED,
    PROPOSED,
    propose_morning_brief,
)
from pm.approval.service import (
    SEND_FAILED,
    SENT,
    ApprovalPolicy,
    auto_approve_and_send,
    load_approval_policy,
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
    delivery_status: str = NOT_ATTEMPTED  # proposed | already_proposed | failed | not_attempted
    delivery_detail: str = ""
    proposal_id: str | None = None


def run_morning_brief_job(
    config: ProjectScheduleConfig,
    gateway,
    *,
    moment: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    publisher=None,
    policy: ApprovalPolicy | None = None,
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

    status, detail, proposal_id = _propose(config, brief, local_day.isoformat(), taken_at, db_path, publisher, policy)
    return MorningBriefJobResult(
        channel_id=config.channel_id,
        taken_at=taken_at,
        status=GENERATED,
        detail="morning brief generated and grounded",
        brief=brief,
        delivery_status=status,
        delivery_detail=detail,
        proposal_id=proposal_id,
    )


def _propose(config, brief, local_date, taken_at, db_path, publisher, policy):
    """Put the finished brief up for approval -- and, when auto-approve is on
    and the brief is safe to send unattended, have the system approve it. A
    failure here is reported, not raised: the brief is already made."""
    try:
        proposal, created = propose_morning_brief(
            brief, config, local_date=local_date, taken_at=taken_at, db_path=db_path
        )
    except Exception as exc:  # noqa: BLE001 - a failed proposal must not take the scheduled job down
        return FAILED, f"{type(exc).__name__}: {exc}", None
    if not created:
        return ALREADY_PROPOSED, f"a proposal for {local_date} already exists (status {proposal.status})", proposal.id

    waiting = f"awaiting approval (proposal {proposal.id})"
    try:
        policy = policy if policy is not None else load_approval_policy()
    except Exception as exc:  # noqa: BLE001 - if the policy cannot be read, nothing is sent unattended
        return PROPOSED, f"{waiting}; auto-approve not applied: {type(exc).__name__}: {exc}", proposal.id
    if not policy.auto_approve:
        return PROPOSED, waiting, proposal.id

    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - a misconfigured publisher leaves it waiting for a person
        return PROPOSED, f"{waiting}; auto-approve not applied: {type(exc).__name__}: {exc}", proposal.id
    outcome = auto_approve_and_send(proposal.id, publisher=publisher, policy=policy, db_path=db_path)
    if outcome.outcome == SENT:
        return AUTO_SENT, f"auto-approved and sent (proposal {proposal.id})", proposal.id
    if outcome.outcome == SEND_FAILED:
        return AUTO_SEND_FAILED, f"auto-approved but the send failed: {outcome.detail}", proposal.id
    return PROPOSED, f"{waiting}; held for a person: {outcome.detail}", proposal.id
