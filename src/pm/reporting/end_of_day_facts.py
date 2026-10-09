"""PM-22: the facts of the end-of-day summary, computed from the stored diff.

The input is a SnapshotDelta (pm.state.diff.compute_delta over the day's morning and end-of-day
snapshots), which holds one entry per item that GENUINELY changed, however many times it moved.
Every such item is placed in exactly one section, by what happened to it (first match wins):

  shipped          it ended `done` and was not done this morning
  newly_blocked    it ended `blocked` and was not blocked this morning
  still_pending    its status moved and it ended open (backlog, in_progress, in_review)
  other_changes    anything else that changed: churn that ended where it began, a new open item,
                   an item gone from the tracker, a handover with no status move

So an item that moved twice (blocked, then done) is one entry under what shipped, with its path in
the line; it is never also under newly blocked. Items that did not change are not in the summary at
all: that is the morning brief's job. They are only counted (`unchanged_open_count`).

Everything here is arithmetic and lookup: no model is involved.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic import BaseModel

from pm.reporting.facts import _person_names
from pm.state.diff import STATUS_CHANGED, ItemDelta, SnapshotDelta
from pm.state.moments import parse_moment
from pm.state.snapshot import UNMAPPED, ProjectSnapshot

SHIPPED = "shipped"
STILL_PENDING = "still_pending"
NEWLY_BLOCKED = "newly_blocked"
OTHER_CHANGES = "other_changes"
SECTION_ORDER = (SHIPPED, STILL_PENDING, NEWLY_BLOCKED, OTHER_CHANGES)

_OPEN_STATUSES = frozenset({"backlog", "in_progress", "in_review"})


class EodItem(BaseModel):
    item_id: str
    title: str
    assignee_id: str | None
    assignee_name: str | None
    kind: str  # the diff's own kind: added / removed / status_changed / flapped / reassigned
    before_status: str | None
    after_status: str | None
    transitions_in_window: int
    detail: str  # the one fact the model is asked to word, and the text shown if its wording is refused
    section: str


class EndOfDayFacts(BaseModel):
    local_date: str
    timezone: str
    morning_taken_at: str
    evening_taken_at: str
    morning_source: str = "stored"  # stored | reconstructed (no morning snapshot was stored for the day)
    sections: dict[str, list[EodItem]]
    unchanged_open_count: int
    unchanged_unmapped: list[str] = []  # items whose status the tracker does not map, as "PM-022 ('waiting_on_vendor')": not known to be open, so not counted as open (the summary counts them; it names only what changed)

    @property
    def changed_ids(self) -> list[str]:
        return [item.item_id for key in SECTION_ORDER for item in self.sections[key]]


def section_for(delta: ItemDelta) -> str:
    before, after = delta.before_status, delta.after_status
    if after == "done" and before != "done":
        return SHIPPED
    if after == "blocked" and before != "blocked":
        return NEWLY_BLOCKED
    if delta.kind == STATUS_CHANGED and after in _OPEN_STATUSES:
        return STILL_PENDING
    return OTHER_CHANGES


def compute_end_of_day_facts(
    delta: SnapshotDelta, before: ProjectSnapshot, after: ProjectSnapshot, *, morning_source: str = "stored"
) -> EndOfDayFacts:
    names = _person_names(after)
    titles = {item.id: item.title for item in before.items} | {item.id: item.title for item in after.items}
    owners = {item.id: item.assignee_id for item in before.items} | {item.id: item.assignee_id for item in after.items}
    sections: dict[str, list[EodItem]] = {key: [] for key in SECTION_ORDER}
    for entry in delta.items:  # already one per item and sorted by item id
        owner_id = owners.get(entry.item_id)
        owner = names.get(owner_id, owner_id) if owner_id else None
        title = titles.get(entry.item_id, entry.item_id)
        detail = f'{entry.description} Title: "{title}". Owner: {owner or "unassigned"}.'
        key = section_for(entry)
        sections[key].append(EodItem(
            item_id=entry.item_id, title=title, assignee_id=owner_id, assignee_name=owner, kind=entry.kind,
            before_status=entry.before_status, after_status=entry.after_status,
            transitions_in_window=entry.transitions_in_window, detail=detail, section=key,
        ))
    changed = {entry.item_id for entry in delta.items}
    unchanged_open = sum(1 for item in after.items if item.status not in ("done", UNMAPPED) and item.id not in changed)
    unchanged_unmapped = [f"{item.id} ({item.raw_status!r})" for item in after.items if item.status == UNMAPPED and item.id not in changed]
    local_date = parse_moment(after.taken_at).astimezone(ZoneInfo(after.timezone)).date().isoformat()
    return EndOfDayFacts(
        local_date=local_date, timezone=after.timezone, morning_taken_at=before.taken_at, evening_taken_at=after.taken_at,
        morning_source=morning_source, sections=sections, unchanged_open_count=unchanged_open, unchanged_unmapped=unchanged_unmapped,
    )
