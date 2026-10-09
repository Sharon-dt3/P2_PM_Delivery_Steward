"""Adding a sprint to the tracker and moving items into it: the two things an operator does by hand, done with checks and an audit record.

A sprint is a name and an inclusive date range. Nothing in this system picks a sprint on its own: new items from a channel go to the sprint that covers
their day, or, when none does, to the sprint the operator NAMED in PM_TRACKER_DEFAULT_SPRINT. So when a sprint ends and the next one has not been added,
the weekly report has no sprint to measure and new items fall back to the named default. Adding the next sprint is what keeps both right.

  add_sprint   refuses a bad id or name, dates that are not dates, an end before the start, an id already in use, and a range that overlaps another
               sprint (two sprints covering one day would make "the sprint that covers the day" ambiguous)
  move_items   moves named items into an existing sprint; refuses an unknown sprint or item; an item already there is left alone

Both write to the audit log (entity type `sprint` / `item`) with who did it and what changed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from pm.storage.db import get_connection

_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class TrackerAdminRefused(Exception):
    """The change was not made, with the reason in words."""


@dataclass(frozen=True)
class MoveResult:
    moved: list[str]
    already_there: list[str]


def _audit(conn, actor: str, action: str, entity_type: str, entity_id: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit (actor, action, entity_type, entity_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (actor, action, entity_type, entity_id, json.dumps(details), datetime.now(timezone.utc).isoformat()),
    )


def _day(value: str, what: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise TrackerAdminRefused(f"{what} must be a date like 2026-10-05; got {value!r}") from None


def add_sprint(db_path: str | Path, *, sprint_id: str, name: str, start: str, end: str, by: str = "operator") -> None:
    if not _ID.match(sprint_id or ""):
        raise TrackerAdminRefused(f"the sprint id must be lower-case letters, digits and hyphens, like sprint-14; got {sprint_id!r}")
    if not (name or "").strip():
        raise TrackerAdminRefused("the sprint needs a name")
    first, last = _day(start, "the start"), _day(end, "the end")
    if last < first:
        raise TrackerAdminRefused(f"the sprint ends ({end}) before it starts ({start})")
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT id, display_name, start_date, end_date FROM sprints").fetchall()
        if any(row[0] == sprint_id for row in existing):
            raise TrackerAdminRefused(f"{sprint_id} already exists")
        for row in existing:
            if first <= date.fromisoformat(row[3]) and date.fromisoformat(row[2]) <= last:
                raise TrackerAdminRefused(f"{start} to {end} overlaps {row[1]} ({row[2]} to {row[3]}): two sprints would cover the same day")
        conn.execute("INSERT INTO sprints (id, display_name, start_date, end_date) VALUES (?, ?, ?, ?)", (sprint_id, name.strip(), start, end))
        _audit(conn, by, "sprint.added", "sprint", sprint_id, {"name": name.strip(), "start": start, "end": end})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def move_items(db_path: str | Path, item_ids: list[str], *, sprint_id: str, by: str = "operator") -> MoveResult:
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM sprints WHERE id = ?", (sprint_id,)).fetchone() is None:
            raise TrackerAdminRefused(f"there is no sprint {sprint_id!r}")
        moved: list[str] = []
        already: list[str] = []
        for item_id in item_ids:
            row = conn.execute("SELECT sprint_id FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise TrackerAdminRefused(f"there is no item {item_id!r}; nothing was moved")
            if row[0] == sprint_id:
                already.append(item_id)
                continue
            conn.execute("UPDATE items SET sprint_id = ? WHERE id = ?", (sprint_id, item_id))
            _audit(conn, by, "item.moved_sprint", "item", item_id, {"from": row[0], "to": sprint_id})
            moved.append(item_id)
        conn.commit()
        return MoveResult(moved, already)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
