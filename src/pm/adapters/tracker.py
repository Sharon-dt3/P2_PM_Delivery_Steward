"""
Tracker adapter -- interface (PM-04).

Narrow interface for everything this agent needs from the delivery
tracker: reading items (optionally filtered), reading one item,
commenting on one, filing a new one, and moving one to a new status.
Agent logic depends ONLY on this interface -- no SQL, no specific
tracker product's shape, may leak past it. Mirrors the ABC-interface /
pydantic-model-for-data / mock-and-real-implementations-behind-it
convention P1 itself uses (see e.g.
../P3_Agents/packages/spine/src/spine/adapters/teams_reader.py).

TrackerMock is the only implementation so far, over this repo's own
seeded fixture (PM-01/02, src/pm/seed/build.py). A real tracker's HTTP
client would sit behind this same interface later without any caller
needing to change.

The master plan's own edge cases for this adapter, and where each one
is actually exercised:
  - missing assignee    -> PM-018 in the seed (PM-03 difficulty 4)
  - stale timestamp      -> PM-014's blocked_since (PM-03 difficulty 1)
  - duplicate item        -> create_item() raises DuplicateItemError
                              (tests/unit/test_tracker_adapter.py)
  - free-text status      -> PM-022 in the seed (PM-03 difficulty 8)
  - no due date            -> TrackerItem has no due_date field at all --
                              tracker items in this domain don't carry
                              one (sprints and commitments do); the
                              adapter's job is to not assume every item
                              has one, which holds vacuously since the
                              model never models one.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel

from pm.storage.db import DEFAULT_DB_PATH, get_connection


class TrackerItem(BaseModel):
    id: str
    title: str
    status: str  # canonical or free text -- see this module's own docstring
    sprint_id: str
    assignee_id: str | None = None
    created_at: str  # ISO date
    blocked_since: str | None = None
    source_message_id: str | None = None


class ItemComment(BaseModel):
    id: int
    item_id: str
    author_id: str | None = None
    body: str
    tags: list[str] = []
    created_at: str


class ItemFilter(BaseModel):
    """Every set field is AND-ed together; an unset (None/False) field
    matches everything. status/assignee_id/sprint_id are plain string
    equality, deliberately not constrained to a canonical enum -- a
    filter has to be able to ask for the free-text-status edge case
    (e.g. status="waiting_on_vendor") the same as any other value."""

    sprint_id: str | None = None
    assignee_id: str | None = None
    status: str | None = None
    unassigned_only: bool = False


class ItemNotFoundError(Exception):
    """Raised by get_item()/add_comment()/transition() for an unknown
    item id."""


class DuplicateItemError(Exception):
    """Raised by create_item() when payload.id already names an
    existing item -- the master plan's own "duplicate item" edge case."""


class Tracker(ABC):
    """Read/write access to the delivery tracker. A narrow interface,
    not a passthrough for whatever a real tracker product's API happens
    to expose."""

    @abstractmethod
    def list_items(self, item_filter: ItemFilter | None = None) -> list[TrackerItem]: ...

    @abstractmethod
    def get_item(self, item_id: str) -> TrackerItem:
        """Raises ItemNotFoundError if item_id doesn't exist."""

    @abstractmethod
    def add_comment(self, item_id: str, body: str, tags: list[str] | None = None) -> ItemComment:
        """Raises ItemNotFoundError if item_id doesn't exist."""

    @abstractmethod
    def create_item(self, payload: TrackerItem) -> TrackerItem:
        """Raises DuplicateItemError if payload.id already exists."""

    @abstractmethod
    def transition(self, item_id: str, to_status: str) -> TrackerItem:
        """Moves item_id to to_status -- any string, not constrained to
        a canonical enum (see ItemFilter's own note) -- recording one
        item_transitions row, and returns the updated item. Sets
        blocked_since to today when to_status == "blocked", and clears
        it otherwise. Raises ItemNotFoundError if item_id doesn't
        exist."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_item(row: sqlite3.Row) -> TrackerItem:
    return TrackerItem(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        sprint_id=row["sprint_id"],
        assignee_id=row["assignee_id"],
        created_at=row["created_at"],
        blocked_since=row["blocked_since"],
        source_message_id=row["source_message_id"],
    )


class TrackerMock(Tracker):
    """The only Tracker implementation so far: a mock over this repo's
    own seeded SQLite fixture (PM-01/02). Opens and closes its own
    connection per call -- no connection is held open across calls,
    same posture P1's own storage/*_repo.py stores take."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = get_connection(self._db_path)
        try:
            yield conn
        finally:
            conn.close()

    def list_items(self, item_filter: ItemFilter | None = None) -> list[TrackerItem]:
        item_filter = item_filter or ItemFilter()
        clauses: list[str] = []
        params: list[object] = []
        if item_filter.sprint_id is not None:
            clauses.append("sprint_id = ?")
            params.append(item_filter.sprint_id)
        if item_filter.assignee_id is not None:
            clauses.append("assignee_id = ?")
            params.append(item_filter.assignee_id)
        if item_filter.status is not None:
            clauses.append("status = ?")
            params.append(item_filter.status)
        if item_filter.unassigned_only:
            clauses.append("assignee_id IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM items {where} ORDER BY id", params).fetchall()
        return [_row_to_item(row) for row in rows]

    def get_item(self, item_id: str) -> TrackerItem:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ItemNotFoundError(item_id)
        return _row_to_item(row)

    def add_comment(self, item_id: str, body: str, tags: list[str] | None = None) -> ItemComment:
        tags = tags or []
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            if exists is None:
                raise ItemNotFoundError(item_id)
            created_at = _now_iso()
            cursor = conn.execute(
                "INSERT INTO item_comments (item_id, author_id, body, tags, created_at) VALUES (?, NULL, ?, ?, ?)",
                (item_id, body, json.dumps(tags), created_at),
            )
            conn.commit()
            comment_id = cursor.lastrowid
        return ItemComment(id=comment_id, item_id=item_id, author_id=None, body=body, tags=tags, created_at=created_at)

    def create_item(self, payload: TrackerItem) -> TrackerItem:
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM items WHERE id = ?", (payload.id,)).fetchone()
            if exists is not None:
                raise DuplicateItemError(payload.id)
            conn.execute(
                """
                INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.id,
                    payload.title,
                    payload.status,
                    payload.sprint_id,
                    payload.assignee_id,
                    payload.created_at,
                    payload.blocked_since,
                    payload.source_message_id,
                ),
            )
            conn.commit()
        return payload

    def transition(self, item_id: str, to_status: str) -> TrackerItem:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise ItemNotFoundError(item_id)
            from_status = row["status"]
            changed_at = _now_iso()
            conn.execute(
                "INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?)",
                (item_id, from_status, to_status, changed_at),
            )
            blocked_since = changed_at[:10] if to_status == "blocked" else None
            conn.execute("UPDATE items SET status = ?, blocked_since = ? WHERE id = ?", (to_status, blocked_since, item_id))
            conn.commit()
            updated_row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(updated_row)
