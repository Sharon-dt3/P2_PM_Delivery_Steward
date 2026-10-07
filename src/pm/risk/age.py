"""PM-19: how long a blocker has been open, computed from its status transitions.

The age is the number of calendar days an item has been in `blocked` continuously, as of a
moment: from the transition that put it there (the latest entry into the current blocked run,
so a flap resets the clock) to the project's local date at that moment. It is read from the
tracker's transition history, never from the stored `blocked_since` field, which can be stale
or wrong; the function does not take that field at all.

Dates are handled the way the rest of the system does: a date-only transition ("2026-09-14",
which is what the seed records) is that date as written; a full timestamp is converted to the
project's timezone first. Where the history cannot give an honest answer (the tracker says
blocked but the last recorded move was to something else) the age is unknown, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pm.adapters.tracker import TransitionRecord
from pm.state.moments import parse_moment

BLOCKED = "blocked"
FROM_TRANSITION = "transition"
FROM_CREATION = "created"


@dataclass(frozen=True)
class BlockedAge:
    blocked: bool
    days: int | None
    entered_at: str | None = None  # the transition's timestamp, as recorded
    entered_on: str | None = None  # the local date it entered blocked
    from_status: str | None = None  # what it was before
    source: str = "none"  # transition | created | none
    unknown: bool = False
    note: str = ""


def _local_date(stamp: str, tz: str) -> date:
    if "T" not in stamp:
        return date.fromisoformat(stamp)  # a plain date is the date as written
    return parse_moment(stamp).astimezone(ZoneInfo(tz)).date()


def compute_blocked_age(
    transitions: list[TransitionRecord],
    *,
    created_at: str,
    current_status: str,
    as_of: datetime,
    tz: str,
) -> BlockedAge:
    """`transitions`: the item's whole history, oldest first. `current_status`: the item's
    status AS OF `as_of` (what the snapshot shows), used to tell a blocked item from one that is
    not and to catch a history that disagrees with it."""
    seen = [t for t in transitions if parse_moment(t.changed_at) <= as_of]
    if seen:
        derived = seen[-1].to_status
    elif transitions:
        derived = transitions[0].from_status
    else:
        derived = current_status

    if derived != current_status and current_status == BLOCKED:
        return BlockedAge(
            blocked=True, days=None, unknown=True,
            note=f"the tracker says blocked but its history ends in {derived!r}; the history does not agree, so no age is given",
        )
    if derived != BLOCKED:
        return BlockedAge(blocked=False, days=None)

    run = []
    for record in reversed(seen):
        if record.to_status != BLOCKED:
            break
        run.append(record)
    if run:
        entry = run[-1]  # the earliest of the final run of entries into blocked
        entered_at, from_status, source = entry.changed_at, entry.from_status, FROM_TRANSITION
    else:
        entered_at, from_status, source = created_at, None, FROM_CREATION  # blocked since it was created

    entered_on = _local_date(entered_at, tz)
    today = as_of.astimezone(ZoneInfo(tz)).date()
    days = (today - entered_on).days
    if days < 0:
        return BlockedAge(blocked=True, days=None, unknown=True, note="it entered blocked after the moment asked about")
    return BlockedAge(
        blocked=True, days=days, entered_at=entered_at, entered_on=entered_on.isoformat(),
        from_status=from_status, source=source,
    )
