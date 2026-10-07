"""The commitment pass the morning job runs when PM_COMMITMENT_FOLLOWUP=1: read the latest channel outcome records,
then follow up every open commitment. Off by default, like every part of this agent that can message a person.
It never raises: the brief is already made, and a failure here must not take the scheduled job down."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from pm.adapters.teams import P1_REPO_ROOT, get_teams_publisher
from pm.adapters.tracker import ItemNotFoundError, TrackerMock
from pm.approval.service import ApprovalPolicy, load_approval_policy
from pm.commitments.followup import (
    FollowupResult,
    load_followup_settings,
    run_followups,
)
from pm.commitments.outcomes import ingest_recent_outcomes, message_lookup
from pm.commitments.store import CommitmentTracker
from pm.seed.build import CHANNEL_ID
from pm.storage.db import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

ENV_ENABLED = "PM_COMMITMENT_FOLLOWUP"
ENV_OUTCOMES_DIR = "PM_OUTCOMES_DIR"
ENV_OUTCOMES_DAYS = "PM_OUTCOMES_DAYS_BACK"
DEFAULT_OUTCOMES_DAYS_BACK = 7


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "0") == "1"


def outcomes_dir() -> Path:
    return Path(os.environ.get(ENV_OUTCOMES_DIR) or (P1_REPO_ROOT / "outcomes"))


def run_commitment_pass(
    moment: datetime, *, db_path: str | Path = DEFAULT_DB_PATH, publisher=None, policy: ApprovalPolicy | None = None,
    channel_id: str = CHANNEL_ID, dry_run: bool = False, ingest: bool = True,
) -> list[FollowupResult]:
    """Ingest the recent outcome records, then run the follow-ups, as of `moment`."""
    settings = load_followup_settings(channel_id)
    tracker_items = TrackerMock(db_path=db_path)
    names = {a.id: a.display_name for a in tracker_items.list_assignees()}

    def item_status(item_id: str) -> str | None:
        try:
            return tracker_items.get_item(item_id).status
        except ItemNotFoundError:
            return None

    if ingest and not dry_run:
        from zoneinfo import ZoneInfo

        raw_days = (os.environ.get(ENV_OUTCOMES_DAYS) or "").strip()
        ingest_recent_outcomes(
            channel_id=channel_id, today=moment.astimezone(ZoneInfo(settings.timezone)).date(),
            days_back=int(raw_days) if raw_days.isdigit() else DEFAULT_OUTCOMES_DAYS_BACK, output_dir=outcomes_dir(),
            tracker=CommitmentTracker(db_path), message_info=message_lookup(channel_id, db_path), roster=set(names),
            item_exists=lambda i: item_status(i) is not None,
        )
    return run_followups(
        moment=moment, settings=settings, publisher=publisher if publisher is not None else get_teams_publisher(), db_path=db_path,
        policy=policy if policy is not None else load_approval_policy(), item_status=item_status, names=names, dry_run=dry_run,
    )


def run_commitment_pass_safely(moment: datetime, *, db_path, publisher=None, policy=None) -> int:
    """For the scheduled job: how many reminders or escalations were sent or are waiting for a person; 0 if switched off
    or if anything went wrong (logged)."""
    if not enabled():
        return 0
    try:
        results = run_commitment_pass(moment, db_path=db_path, publisher=publisher, policy=policy)
    except Exception as exc:  # noqa: BLE001 - a failed follow-up must not take the scheduled job down
        logger.warning("commitment_followup_failed error=%s: %s", type(exc).__name__, exc)
        return 0
    return sum(1 for r in results if r.action in ("nudge", "escalation") and r.status in ("sent", "awaiting_approval"))
