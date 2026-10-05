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

from datetime import datetime

from pydantic import BaseModel

from pm.adapters.tracker import Tracker, TransitionRecord
from pm.seed.build import CANONICAL_STATUSES
from pm.state.moments import parse_moment as _parse_moment
from pm.state.snapshot import UNMAPPED, ProjectSnapshot

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
    # Set only when the item's owner differs between the two snapshots, on
    # any kind of delta: a status change or a flap can also be a handover.
    before_assignee: str | None = None
    after_assignee: str | None = None
    description: str


class SnapshotDelta(BaseModel):
    before_taken_at: str
    after_taken_at: str
    items: list[ItemDelta]


def _normalize(status: str) -> str:
    """The same UNMAPPED convention state.snapshot.normalize_item() applies
    to a live tracker read, applied here to a reconstructed historical
    status so before/after values stay comparable to a NormalizedItem's own
    `status` field."""
    return status if status in CANONICAL_STATUSES else UNMAPPED


def _status_as_of(history: list[TransitionRecord], moment: datetime, *, fallback: str) -> str:
    """The item's status as of `moment`, reconstructed from its transition
    history: the to_status of the last transition at or before moment.
    Before its first transition an item is in that transition's own
    from_status -- whatever status it was actually created in, not an
    assumed default. An item with no transitions at all has only ever had
    one status, the one its snapshot recorded (`fallback`)."""
    status = history[0].from_status if history else fallback
    for record in history:
        if _parse_moment(record.changed_at) <= moment:
            status = record.to_status
    return status


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
        before_item = before_by_id[item_id]
        after_item = after_by_id[item_id]
        before_status = _normalize(_status_as_of(history, window_start, fallback=before_item.status))
        after_status = _normalize(_status_as_of(history, window_end, fallback=after_item.status))
        in_window = [
            record for record in history if window_start < _parse_moment(record.changed_at) <= window_end
        ]
        path = " -> ".join([in_window[0].from_status] + [record.to_status for record in in_window]) if in_window else ""

        # An owner change is reported on whatever kind of delta the item
        # gets (still one entry per item), never dropped because the status
        # moved as well.
        reassigned = before_item.assignee_id != after_item.assignee_id
        owner = (
            {"before_assignee": before_item.assignee_id, "after_assignee": after_item.assignee_id}
            if reassigned
            else {}
        )
        owner_note = (
            f" It was also reassigned from {before_item.assignee_id!r} to {after_item.assignee_id!r}."
            if reassigned
            else ""
        )

        if before_status != after_status:
            # More than one transition means it bounced on the way: say so,
            # with the actual path, rather than presenting it as a single move.
            moved = f"{item_id} moved from {before_status} to {after_status}"
            description = (
                f"{moved} after {len(in_window)} transitions in the window ({path})."
                if len(in_window) > 1
                else f"{moved}."
            )
            deltas.append(
                ItemDelta(
                    item_id=item_id,
                    kind=STATUS_CHANGED,
                    before_status=before_status,
                    after_status=after_status,
                    transitions_in_window=len(in_window),
                    description=description + owner_note,
                    **owner,
                )
            )
            continue

        if len(in_window) > 1:
            # Net status unchanged, but it moved and came back within the
            # window -- PM-016's own planted difficulty. Reported once,
            # not once per transition row, with the actual path so the
            # churn is legible rather than just asserted.
            deltas.append(
                ItemDelta(
                    item_id=item_id,
                    kind=FLAPPED,
                    before_status=before_status,
                    after_status=after_status,
                    transitions_in_window=len(in_window),
                    description=(
                        f"{item_id} churned ({path}) and ended back at {after_status} -- "
                        "net status unchanged, but it was not quiet." + owner_note
                    ),
                    **owner,
                )
            )
            continue

        if reassigned:
            deltas.append(
                ItemDelta(
                    item_id=item_id,
                    kind=REASSIGNED,
                    before_status=before_status,
                    after_status=after_status,
                    description=f"{item_id} reassigned from {before_item.assignee_id!r} to {after_item.assignee_id!r}.",
                    **owner,
                )
            )

    deltas.sort(key=lambda delta: delta.item_id)
    return SnapshotDelta(before_taken_at=before.taken_at, after_taken_at=after.taken_at, items=deltas)
