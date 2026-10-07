"""The ageing view: every open commitment, how long it has been open, and how overdue it is.

A commitment is open until it is closed, or until its item reaches `done` (the tracker is the truth about
the work). Each row says who, what, when it was made, when it was due, how many days it has been open, how many
days overdue (or until due), what has been done about it (nudged, escalated), and a bucket to sort by. Most
overdue first. Pure arithmetic over the store: no model.

A commitment with no day to hold it to ("I'll get to it") is listed in its own bucket, never guessed at.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from pm.commitments.outcomes import resolve_due
from pm.commitments.store import (
    ESCALATED,
    NUDGED,
    OPEN,
    CommitmentTracker,
    TrackedCommitment,
)

BUCKETS = ("overdue more than 7 days", "overdue 3 to 7 days", "overdue 1 to 2 days", "due today", "due in 1 to 3 days", "due later", "no due date")


@dataclass(frozen=True)
class AgeingRow:
    commitment: TrackedCommitment
    owner: str
    due: str | None  # the day it is held to: the stated date, or the phrase resolved against the day it was said
    open_days: int
    days_to_due: int | None  # >= 0 when not yet overdue
    overdue_days: int | None  # >= 1 when overdue
    bucket: str
    item_status: str | None
    nudged: bool
    escalated: bool


def effective_due(commitment: TrackedCommitment) -> str | None:
    """The stated date, else the relative phrase resolved against the day it was made (so 'end of week' is a Friday)."""
    if commitment.due_date_iso:
        return commitment.due_date_iso
    if commitment.due_date_text:
        return resolve_due(commitment.due_date_text, date.fromisoformat(commitment.made_at[:10]))[0]
    return None


def _bucket(days_to_due: int | None, overdue: int | None) -> str:
    if overdue is not None:
        return BUCKETS[0] if overdue > 7 else BUCKETS[1] if overdue >= 3 else BUCKETS[2]
    if days_to_due is None:
        return BUCKETS[6]
    return BUCKETS[3] if days_to_due == 0 else BUCKETS[4] if days_to_due <= 3 else BUCKETS[5]


def is_open(commitment: TrackedCommitment, item_status: Callable[[str], str | None]) -> bool:
    if commitment.status != OPEN:
        return False
    return not (commitment.item_id and item_status(commitment.item_id) == "done")


def ageing_view(
    *, tracker: CommitmentTracker, today: date, item_status: Callable[[str], str | None] = lambda _id: None,
    names: dict[str, str] | None = None,
) -> list[AgeingRow]:
    names = names or {}
    rows = []
    for commitment in tracker.list(status=OPEN):
        if not is_open(commitment, item_status):
            continue
        due_text = effective_due(commitment)
        delta = (date.fromisoformat(due_text) - today).days if due_text else None
        overdue = -delta if delta is not None and delta < 0 else None
        to_due = delta if delta is not None and delta >= 0 else None
        events = {e["kind"] for e in tracker.events(commitment.id)}
        rows.append(AgeingRow(
            commitment=commitment, owner=names.get(commitment.member_id, commitment.member_id), due=due_text,
            open_days=(today - date.fromisoformat(commitment.made_at[:10])).days, days_to_due=to_due, overdue_days=overdue,
            bucket=_bucket(to_due, overdue), item_status=item_status(commitment.item_id) if commitment.item_id else None,
            nudged=NUDGED in events, escalated=ESCALATED in events,
        ))
    return sorted(rows, key=lambda r: (BUCKETS.index(r.bucket), -(r.overdue_days or 0), r.days_to_due if r.days_to_due is not None else 10**6, r.commitment.id))


def format_ageing(rows: list[AgeingRow], today: date) -> str:
    lines = [f"Open commitments as of {today.isoformat()}: {len(rows)}"]
    if not rows:
        return lines[0] + "\n  none."
    for bucket in BUCKETS:
        group = [r for r in rows if r.bucket == bucket]
        if not group:
            continue
        lines.append(f"\n  {bucket} ({len(group)})")
        for r in group:
            when = f"overdue {r.overdue_days}d" if r.overdue_days else (f"due in {r.days_to_due}d" if r.days_to_due else "due today") if r.due else "no date"
            done = ", ".join(x for x, flag in (("nudged", r.nudged), ("escalated", r.escalated)) if flag)
            lines.append(f"    #{r.commitment.id:<3} {r.owner:<16} {when:<13} open {r.open_days}d  {r.commitment.text[:70]}"
                         + (f"  [{r.commitment.item_id}: {r.item_status}]" if r.commitment.item_id else "") + (f"  ({done})" if done else ""))
    return "\n".join(lines)
