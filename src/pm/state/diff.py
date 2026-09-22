"""
Snapshot diff engine (PM-06).

Computes what actually changed between two ProjectSnapshots (PM-05). The
one thing that makes this more than a field-by-field comparison of the two
snapshots' NormalizedItems: a ProjectSnapshot only ever carries an item's
CURRENT status as of when it was taken, so on a static, already-seeded
dataset two snapshots taken moments apart both see the same final status
for every item -- there is nothing to diff. What actually changed has to
be reconstructed from the tracker's own transition history (PM-04's
list_transitions()) as of each snapshot's own `taken_at` moment, which is
also the only way to see churn that nets out to nothing: PM-016's own
planted difficulty (PM-03 difficulty 2/10) moves to done and back to
in_progress hours later, same calendar day -- its status as of the morning
and as of end-of-day are identical, so a snapshot-to-snapshot field
comparison would miss it entirely. Reconstructing status from history, and
separately counting every transition that landed strictly inside the
window between the two snapshots, is what surfaces it.

_parse_moment() exists because changed_at values in this system mix
date-only strings ("2026-09-16", most seed rows) and full ISO timestamps
("2026-09-16T15:30:00", the rows that need same-day ordering -- see
pm.adapters.tracker.TransitionRecord's own docstring). Plain string
ordering across that mix is not reliable, so every comparison in this
module goes through real datetime objects instead.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from pydantic import BaseModel

from pm.adapters.tracker import Tracker, TransitionRecord
from pm.seed.build import CANONICAL_STATUSES
from pm.state.snapshot import UNMAPPED, ProjectSnapshot

# The status every item starts in before its first recorded transition --
# this repo's own seeding convention (pm.seed.build._history()'s prev_status
# starts here; the two items with zero transitions at all, PM-013 and
# PM-021, are seeded directly in this status and never leave it).
INITIAL_STATUS = "backlog"

ADDED = "added"
REMOVED = "removed"
STATUS_CHANGED = "status_changed"
FLAPPED = "flapped"
REASSIGNED = "reassigned"


class ItemDelta(BaseModel):
    item_id: str
    kind: str  # one of ADDED / REMOVED / STATUS_CHANGED / FLAPPED / REASSIGNED
    before_status: str | None = None
    after_status: str | None = None
    transitions_in_window: int = 0
    description: str


class SnapshotDelta(BaseModel):
    before_taken_at: str
    after_taken_at: str
    items: list[ItemDelta]


def _parse_moment(value: str) -> datetime:
    """Parses a changed_at/taken_at value -- date-only or full ISO
    timestamp, offset or naive -- into a directly comparable, always
    timezone-aware datetime (naive values are treated as UTC, matching
    every writer in this system: TrackerMock._now_iso() and
    state.snapshot._now_iso() both use datetime.now(timezone.utc))."""
    if "T" in value:
        parsed = datetime.fromisoformat(value)
    else:
        parsed = datetime.combine(date.fromisoformat(value), datetime.min.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize(status: str) -> str:
    """The same UNMAPPED convention state.snapshot.normalize_item() applies
    to a live tracker read, applied here to a reconstructed historical
    status so before/after values stay comparable to a NormalizedItem's own
    `status` field."""
    return status if status in CANONICAL_STATUSES else UNMAPPED


def _status_as_of(history: list[TransitionRecord], moment: datetime) -> tuple[str, int]:
    """The item's status as of `moment`, reconstructed from its full
    transition history: the to_status of the last transition at or before
    moment, or INITIAL_STATUS if none has happened yet (or ever). Also
    returns how many transitions in `history` fall strictly after moment
    up to nothing in particular -- callers pass two moments and take the
    count between them separately; this helper only resolves a single
    point-in-time status."""
    status = INITIAL_STATUS
    count_at_or_before = 0
    for record in history:
        if _parse_moment(record.changed_at) <= moment:
            status = record.to_status
            count_at_or_before += 1
    return status, count_at_or_before


def compute_delta(before: ProjectSnapshot, after: ProjectSnapshot, tracker: Tracker) -> SnapshotDelta:
    """The delta between two snapshots, using `tracker` to pull each
    item's full transition history rather than trusting the two
    snapshots' own captured NormalizedItem.status fields (see this
    module's docstring for why). `tracker` should be the same
    adapter/database the two snapshots were themselves built from."""
    before_ids = {item.id for item in before.items}
    after_ids = {item.id for item in after.items}
    window_start = _parse_moment(before.taken_at)
    window_end = _parse_moment(after.taken_at)

    deltas: list[ItemDelta] = []

    for item_id in sorted(after_ids - before_ids):
        after_item = next(item for item in after.items if item.id == item_id)
        deltas.append(
            ItemDelta(
                item_id=item_id,
                kind=ADDED,
                before_status=None,
                after_status=after_item.status,
                description=f"{item_id} is new since the previous snapshot (now {after_item.status}).",
            )
        )

    for item_id in sorted(before_ids - after_ids):
        before_item = next(item for item in before.items if item.id == item_id)
        deltas.append(
            ItemDelta(
                item_id=item_id,
                kind=REMOVED,
                before_status=before_item.status,
                after_status=None,
                description=f"{item_id} is no longer present in the tracker (was {before_item.status}).",
            )
        )

    before_by_id = {item.id: item for item in before.items}
    after_by_id = {item.id: item for item in after.items}

    for item_id in sorted(before_ids & after_ids):
        history = tracker.list_transitions(item_id)
        before_status, _ = _status_as_of(history, window_start)
        after_status, _ = _status_as_of(history, window_end)
        before_status = _normalize(before_status)
        after_status = _normalize(after_status)
        in_window = [
            record for record in history if window_start < _parse_moment(record.changed_at) <= window_end
        ]

        if before_status != after_status:
            deltas.append(
                ItemDelta(
                    item_id=item_id,
                    kind=STATUS_CHANGED,
                    before_status=before_status,
                    after_status=after_status,
                    transitions_in_window=len(in_window),
                    description=f"{item_id} moved from {before_status} to {after_status}.",
                )
            )
            continue

        if len(in_window) > 1:
            # Net status unchanged, but it moved and came back within the
            # window -- PM-016's own planted difficulty. Reported once,
            # not once per transition row, with the actual path so the
            # churn is legible rather than just asserted.
            path = " -> ".join([in_window[0].from_status] + [record.to_status for record in in_window])
            deltas.append(
                ItemDelta(
                    item_id=item_id,
                    kind=FLAPPED,
                    before_status=before_status,
                    after_status=after_status,
                    transitions_in_window=len(in_window),
                    description=(
                        f"{item_id} churned ({path}) and ended back at {after_status} -- "
                        "net status unchanged, but it was not quiet."
                    ),
                )
            )
            continue

        before_item = before_by_id[item_id]
        after_item = after_by_id[item_id]
        if before_item.assignee_id != after_item.assignee_id:
            deltas.append(
                ItemDelta(
                    item_id=item_id,
                    kind=REASSIGNED,
                    before_status=before_status,
                    after_status=after_status,
                    description=f"{item_id} reassigned from {before_item.assignee_id!r} to {after_item.assignee_id!r}.",
                )
            )

    deltas.sort(key=lambda delta: delta.item_id)
    return SnapshotDelta(before_taken_at=before.taken_at, after_taken_at=after.taken_at, items=deltas)
