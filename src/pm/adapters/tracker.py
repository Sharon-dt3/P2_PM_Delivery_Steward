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


class TransitionRecord(BaseModel):
    """One row of an item's transition history (PM-06's own dependency:
    detecting same-day flap-and-return churn, e.g. PM-016, needs the full
    ordered history, not just the item's current status). changed_at is
    whatever transition() wrote -- either a full ISO timestamp (mock's own
    _now_iso()) or the seed data's date-only string -- callers that need to
    compare moments across mixed granularities parse this themselves rather
    than relying on plain string ordering (see pm/state/diff.py's
    _parse_moment())."""

    item_id: str
    from_status: str
    to_status: str
    changed_at: str


class Sprint(BaseModel):
    """Sprint metadata (PM-08's own dependency: "sprint day and scope"
    needs to know which sprint is current as of a given moment, and a
    sprint's own date range, to answer that). A real tracker product
    exposes this alongside items (e.g. Jira's Sprint resource), so it
    lives on this same interface rather than a separate adapter."""

    id: str
    display_name: str
    start_date: str  # ISO date, inclusive
    end_date: str  # ISO date, inclusive


class Assignee(BaseModel):
    """Roster metadata (PM-10's own dependency: reporting absence
    honestly needs to know who is ON the team in the first place, not
    just who happens to already own an item or a commitment -- a
    zero-activity person has neither, so nothing derived from items or
    commitments alone would ever surface them). Lives on this same
    interface, over the same seeded assignees table items/commits/
    commitments already reference, for the same reason Sprint does: a
    real tracker product exposes its own roster alongside items too."""

    id: str
    display_name: str


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

    @abstractmethod
    def list_transitions(self, item_id: str) -> list[TransitionRecord]:
        """Every transition row item_id has ever recorded, oldest first --
        the full history, not just the current status, so a caller can
        detect churn (e.g. a same-day move to done and back) that a single
        before/after status comparison would miss entirely. Returns an
        empty list for an item with no recorded transitions (not an
        error); raises ItemNotFoundError only if item_id itself doesn't
        exist."""

    @abstractmethod
    def list_sprints(self) -> list[Sprint]:
        """Every sprint on record, in no particular guaranteed order --
        callers that need "the current one" (PM-08) resolve that
        themselves from each sprint's own start_date/end_date against
        whatever moment they care about."""

    @abstractmethod
    def list_assignees(self) -> list[Assignee]:
        """Every person on the roster, in id order -- including anyone
        who owns no item, no commitment, and no commit at all (PM-10's
        own "not omitted" requirement: a caller that only ever looked at
        items/commitments would never learn a genuinely silent person
        exists to report on)."""


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


def _row_to_transition(row: sqlite3.Row) -> TransitionRecord:
    return TransitionRecord(
        item_id=row["item_id"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        changed_at=row["changed_at"],
    )


def _row_to_sprint(row: sqlite3.Row) -> Sprint:
    return Sprint(
        id=row["id"],
        display_name=row["display_name"],
        start_date=row["start_date"],
        end_date=row["end_date"],
    )


def _row_to_assignee(row: sqlite3.Row) -> Assignee:
    return Assignee(id=row["id"], display_name=row["display_name"])


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

    def list_transitions(self, item_id: str) -> list[TransitionRecord]:
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            if exists is None:
                raise ItemNotFoundError(item_id)
            # Ordered by id (insertion order), not changed_at: changed_at
            # mixes date-only and full-timestamp strings (see this
            # module's docstring on TransitionRecord), which don't sort
            # reliably against each other as plain strings. Every writer
            # -- this class's own transition(), and the seed data's
            # _history() helper -- appends rows in true chronological
            # order, so the autoincrement id is a faithful proxy for
            # "when it actually happened."
            rows = conn.execute(
                "SELECT * FROM item_transitions WHERE item_id = ? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [_row_to_transition(row) for row in rows]

    def list_sprints(self) -> list[Sprint]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM sprints ORDER BY id").fetchall()
        return [_row_to_sprint(row) for row in rows]

    def list_assignees(self) -> list[Assignee]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM assignees ORDER BY id").fetchall()
        return [_row_to_assignee(row) for row in rows]
