"""The job body for a channel brief (see pm.channelbrief): what a scheduler fires, and what scripts/run_channel_brief.py runs by hand.

Reads the real record, computes the facts, renders the message, and PROPOSES it (pm.channelbrief.proposal): nothing is sent. A person
approves, edits or rejects it through pm.approval.service (a Teams card, the dashboard, scripts/approve.py), and only then is it posted,
through the publisher chosen by TEAMS_PUBLISHER_MODE, to a channel on P1's allowlist, logged and audited. With nothing real to report from
(no record, a stale one, one P1 was not cleared to pass on) it proposes nothing and says why: it never falls back to anything else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from spine.config.calendar import is_working_day

from pm.approval.proposals import NOT_ATTEMPTED
from pm.approval.service import ApprovalPolicy
from pm.channel.gate import log_refusal
from pm.channel.record import RecordRefused
from pm.channelbrief.facts import (
    ChannelBriefFacts,
    NoUsableRecord,
    P1Directory,
    compute_channel_brief_facts,
)
from pm.channelbrief.proposal import propose_channel_brief
from pm.channelbrief.render import render_message
from pm.jobs.proposal_flow import propose_for_approval
from pm.mirror.hook import mirrored
from pm.scheduling.config import ProjectScheduleConfig
from pm.storage.db import DEFAULT_DB_PATH

PROPOSED_FROM_RECORD = "proposed_from_record"
SKIPPED_NON_WORKING_DAY = "skipped_non_working_day"
NO_DATA = "no_data"  # nothing real to make a brief from: nothing is proposed
ENV_LABEL = "PM_CHANNEL_BRIEF_LABEL"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelBriefResult:
    channel_id: str
    channel_name: str
    kind: str
    status: str
    detail: str
    message: str | None = None
    facts: ChannelBriefFacts | None = None
    delivery_status: str = NOT_ATTEMPTED
    delivery_detail: str = ""
    proposal_id: str | None = None


@mirrored
def run_channel_brief_job(
    config: ProjectScheduleConfig,
    channel_name: str,
    kind: str,
    *,
    moment: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    outcomes_dir: Path | None = None,
    directory: P1Directory | None = None,
    dry_run: bool = False,
) -> ChannelBriefResult:
    """`config` carries the channel's own timezone and working days (from P1's channel config); `kind` is "morning" or "evening".
    Idempotent: a second run for the same channel, day and kind finds the proposal the first made. `dry_run` makes the message and
    proposes nothing."""
    resolved_moment = moment or datetime.now(timezone.utc)
    local_day = resolved_moment.astimezone(ZoneInfo(config.timezone)).date()
    base = {"channel_id": config.channel_id, "channel_name": channel_name, "kind": kind}

    if not is_working_day(local_day, config):
        return ChannelBriefResult(**base, status=SKIPPED_NON_WORKING_DAY, detail=f"{local_day} is not a working day for {channel_name}")
    try:
        facts = compute_channel_brief_facts(
            config.channel_id, channel_name, kind, local_day, db_path=db_path, outcomes_dir=outcomes_dir, directory=directory,
        )
    except NoUsableRecord as exc:
        if exc.code not in ("no_record", "record_not_written_yet"):  # a refused record is P1 withholding consent: leave the same row P2's other readers leave
            _log_refused(db_path, config.channel_id, local_day, exc)
        return ChannelBriefResult(**base, status=NO_DATA, detail=f"nothing proposed: {exc.reason}")

    label = os.environ.get(ENV_LABEL, "")
    message = render_message(facts, label=label)
    if dry_run:
        return ChannelBriefResult(**base, status=PROPOSED_FROM_RECORD, detail="dry run: nothing proposed", message=message, facts=facts)
    status, detail, proposal_id = propose_for_approval(
        lambda: propose_channel_brief(facts, target_channel=config.publish_channel_id, label=label, db_path=db_path),
        local_date=facts.local_date, db_path=db_path, publisher=publisher, policy=policy,
    )
    return ChannelBriefResult(
        **base, status=PROPOSED_FROM_RECORD, detail=f"built from P1's record for {facts.record_date}", message=message, facts=facts,
        delivery_status=status, delivery_detail=detail, proposal_id=proposal_id,
    )


def _log_refused(db_path, channel_id: str, day, exc: NoUsableRecord) -> None:
    try:
        log_refusal(db_path, f"{channel_id}/{day}.json", RecordRefused(exc.code, exc.reason), consumer="channel_brief")
    except Exception as err:  # noqa: BLE001 - recording a refusal must never take a job down
        logger.warning("channel_brief_refusal_not_logged error=%s: %s", type(err).__name__, err)
