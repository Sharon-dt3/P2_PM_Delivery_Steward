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

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from spine.config.calendar import is_working_day

from pm.approval.proposals import (
    NOT_ATTEMPTED,
    propose_morning_brief,
)
from pm.approval.service import (
    ApprovalPolicy,
)
from pm.commitments.job import run_commitment_pass_safely
from pm.jobs.proposal_flow import propose_for_approval
from pm.jobs.snapshot_capture import capture_snapshot
from pm.mirror.hook import mirrored
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import MorningBrief, generate_morning_brief
from pm.risk.promotion_config import PromotionConfigError, load_promotion_policy
from pm.risk.proposals import detect_and_propose
from pm.risklog.hook import pull_lead_edits_if_enabled
from pm.scheduling.config import ProjectScheduleConfig
from pm.storage.db import DEFAULT_DB_PATH

SKIPPED_NON_WORKING_DAY = "skipped_non_working_day"
GENERATED = "generated"

logger = logging.getLogger(__name__)


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
    risk_proposals: int = 0  # risk-log entries proposed this run (PM-16; 0 when detection is off)
    commitment_followups: int = 0  # reminders and escalations sent or waiting for a person this run (PM-24; 0 when off)
    risk_log_sync: str = ""  # off | skipped | error | in_sync | pushed | pulled | conflict | ...


@mirrored
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

    risk_log_sync = pull_lead_edits_if_enabled(db_path)  # the lead's risk-log edits, before the brief
    taken_at = resolved_moment.isoformat()
    snapshot = capture_snapshot(config, resolved_moment, db_path=db_path)

    facts = compute_morning_brief_facts(snapshot)
    brief = generate_morning_brief(facts, gateway)

    status, detail, proposal_id = _propose(config, brief, local_day.isoformat(), taken_at, db_path, publisher, policy)
    risk_proposals = _propose_risks(snapshot, gateway, db_path)
    commitment_followups = run_commitment_pass_safely(resolved_moment, db_path=db_path, publisher=publisher, policy=policy)
    return MorningBriefJobResult(
        channel_id=config.channel_id,
        taken_at=taken_at,
        status=GENERATED,
        detail="morning brief generated and grounded",
        brief=brief,
        delivery_status=status,
        delivery_detail=detail,
        proposal_id=proposal_id,
        risk_proposals=risk_proposals,
        commitment_followups=commitment_followups,
        risk_log_sync=risk_log_sync,
    )


def _propose_risks(snapshot, gateway, db_path) -> int:
    """PM-16/PM-19, when PM_RISK_DETECTION=1: propose a risk-log entry for each current blocker
    that has none and is older than the configured threshold (no threshold configured: every
    one). The threshold is read from configuration each run; an unusable configuration proposes
    nothing rather than falling back to a guess. Returns how many NEW proposals were made.
    Never raises: the brief is already made, and a failure here must not take the job down."""
    if os.environ.get("PM_RISK_DETECTION", "0") != "1":
        return 0
    try:
        promotion = load_promotion_policy()
    except PromotionConfigError as exc:
        logger.warning("risk_promotion_config_unusable error=%s", exc)
        return 0
    try:
        return sum(1 for r in detect_and_propose(snapshot, gateway, db_path=db_path, promotion=promotion) if r.created)
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk_detection_failed error=%s: %s", type(exc).__name__, exc)
        return 0


def _propose(config, brief, local_date, taken_at, db_path, publisher, policy):
    """Put the finished brief up for approval (and, when safe, auto-approve it): see pm.jobs.proposal_flow."""
    return propose_for_approval(
        lambda: propose_morning_brief(brief, config, local_date=local_date, taken_at=taken_at, db_path=db_path),
        local_date=local_date, db_path=db_path, publisher=publisher, policy=policy,
    )
