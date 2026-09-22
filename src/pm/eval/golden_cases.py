"""
Golden case 3 (PM-07): hand-labeled ground truth for what actually changed
between the morning of 2026-09-16 and end of that day.

These labels were written by reading pm/seed/build.py's ITEM_TRANSITIONS
directly -- not by running the diff engine and copying its answer. That
independence is the entire point of a golden case: PM-06's compute_delta()
is what gets graded against this, not the other way around.

The window (MORNING, END_OF_DAY) brackets 2026-09-16 itself:
  - PM-016 (PM-03 difficulty 2/10): already in_progress before MORNING
    (moved out of backlog on 2026-09-15), then moved to done at
    2026-09-16T10:00:00 and back to in_progress at 2026-09-16T15:30:00 --
    two transitions inside the window, net status unchanged. A flap, not
    a status change.
  - PM-018 (PM-03 difficulty 4 -- unassigned, but still moves): in_progress
    since 2026-09-10, reaches in_review on 2026-09-16 -- one transition
    inside the window, a real status change.
  - PM-020: created straight into this window -- backlog the whole time
    before MORNING, moved to in_progress on 2026-09-16 -- one transition
    inside the window, a real status change.

Every other seeded item's transitions sit entirely outside this window
(finished in August, or not touched again until after 2026-09-17), so the
hand-labeled set below is deliberately exhaustive: anything the diff
engine reports outside these three item ids is a false positive, and
missing any of these three is a false negative.
"""

from __future__ import annotations

from pm.state.diff import FLAPPED, STATUS_CHANGED

MORNING = "2026-09-15T23:59:59+00:00"
END_OF_DAY = "2026-09-16T23:59:59+00:00"


class HandLabel:
    """A plain (item_id, kind, before_status, after_status) tuple wrapped
    in a class only so golden_cases.py reads as prose, not as a bag of
    positional tuples. Deliberately NOT pm.state.diff.ItemDelta -- these
    labels must not depend on the very model they're grading."""

    def __init__(self, item_id: str, kind: str, before_status: str, after_status: str, note: str) -> None:
        self.item_id = item_id
        self.kind = kind
        self.before_status = before_status
        self.after_status = after_status
        self.note = note

    @property
    def key(self) -> tuple[str, str]:
        """The (item_id, kind) pair runner.py matches predictions against."""
        return (self.item_id, self.kind)


GOLDEN_CASE_3: list[HandLabel] = [
    HandLabel(
        item_id="PM-016",
        kind=FLAPPED,
        before_status="in_progress",
        after_status="in_progress",
        note="Moved to done at 10:00, back to in_progress at 15:30 -- same day, net unchanged.",
    ),
    HandLabel(
        item_id="PM-018",
        kind=STATUS_CHANGED,
        before_status="in_progress",
        after_status="in_review",
        note="Unassigned the whole time (PM-03 difficulty 4) -- still reached in_review.",
    ),
    HandLabel(
        item_id="PM-020",
        kind=STATUS_CHANGED,
        before_status="backlog",
        after_status="in_progress",
        note="Created into this window; first ever transition happens inside it.",
    ),
]
