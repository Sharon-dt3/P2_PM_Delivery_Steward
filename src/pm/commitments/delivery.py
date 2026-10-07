"""What happens at the moment a reminder or an escalation is actually sent, and just after.

The approval service calls these for the two direct-message proposal types, so a reminder a person approves later
obeys the same rules as one the job sends straight away:

- before sending a reminder: the shared per-person per-day cap is checked AGAIN, against today's ledgers, because a
  first reminder waits for approval and can be approved after P1 (or this agent) already chased the same person;
- before sending either: the commitment must still be open: nobody is chased, and no lead is told, about something
  that has since been done;
- after sending: the reminder is entered in this agent's side of the shared ledger (from then on it counts against
  the cap, on the day it actually went) and the follow-up is recorded as an event, once.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from spine.approval.proposals import Proposal

from pm.approval.proposals import ESCALATION_PROPOSAL_TYPE, NUDGE_PROPOSAL_TYPE
from pm.commitments.ageing import is_open
from pm.commitments.cap import (
    SharedCapUnavailableError,
    SharedNudgeCap,
    cap_from_environment,
)
from pm.commitments.store import (
    ESCALATED,
    NUDGED,
    CommitmentNotFoundError,
    CommitmentTracker,
)

DEFAULT_TIMEZONE = "UTC"


def local_day(now: datetime, tz: str | None) -> str:
    return now.astimezone(ZoneInfo(tz or DEFAULT_TIMEZONE)).date().isoformat()


def pre_send_problem(proposal: Proposal, db_path: str | Path, now: datetime) -> str | None:
    """Why this must not be sent right now, in words; None if it may be."""
    payload = proposal.payload
    try:
        commitment = CommitmentTracker(db_path).get(int(payload["commitment_id"]))
    except (CommitmentNotFoundError, KeyError, ValueError):
        return "the commitment this was about is not on record"
    if not is_open(commitment, _item_status(db_path)):
        return "the commitment has been closed since this was planned, so nobody is chased about it"

    if proposal.type != NUDGE_PROPOSAL_TYPE:
        return None
    cap = SharedNudgeCap(db_path=db_path, cap=cap_from_environment(int(payload.get("cap", 1))))
    try:
        reading = cap.reading(payload["member_id"], local_day(now, payload.get("timezone")))
    except SharedCapUnavailableError as exc:
        return f"the shared nudge cap could not be checked, so nothing is sent: {exc}"
    return f"not sent: {reading.describe()}" if reading.reached else None


def after_send(proposal: Proposal, db_path: str | Path, now: datetime) -> None:
    payload = proposal.payload
    day = local_day(now, payload.get("timezone"))
    tracker = CommitmentTracker(db_path)
    commitment_id = int(payload["commitment_id"])
    if proposal.type == NUDGE_PROPOSAL_TYPE:
        SharedNudgeCap(db_path=db_path).mark_sent(payload["ledger_key"], sent_at=now.isoformat(), day=day)
        tracker.record_event(commitment_id, NUDGED, day, f"reminder sent to {payload['member_id']}", proposal.id)
    elif proposal.type == ESCALATION_PROPOSAL_TYPE:
        tracker.record_event(commitment_id, ESCALATED, day, f"escalated to {payload['recipient_id']}", proposal.id)


def _item_status(db_path: str | Path):
    from pm.adapters.tracker import ItemNotFoundError, TrackerMock

    tracker = TrackerMock(db_path=db_path)

    def status(item_id: str) -> str | None:
        try:
            return tracker.get_item(item_id).status
        except ItemNotFoundError:
            return None

    return status
