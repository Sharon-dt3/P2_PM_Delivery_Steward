"""The commitment store: who promised what by when, and what has happened about it since.

The seeded `commitments` table is the starting point (source 'seed'); P1's channel outcome records feed it
(source 'outcome', pm.commitments.outcomes); a person can add or close one by hand (source 'manual'). A
commitment is open until it is closed: fulfilled (its item reached `done`, or someone said so) or cancelled.

Everything that is DONE about a commitment is an event, and each kind of event happens once per commitment
(a database constraint, not a convention): ingested, nudged, overdue, escalated, fulfilled, cancelled.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel

from pm.storage.db import DEFAULT_DB_PATH, get_connection, run_migrations

_UPGRADED: set[str] = set()


def ensure_current(db_path: str | Path) -> None:
    """Bring a database up to the current schema before it is used. Commitment tracking added columns and tables (migration
    0005); a database made before that (the live one) must not need a manual step. The migration is additive and each one is
    applied once, so this is safe to call on every open; it is done once per database per process."""
    key = str(db_path)
    if key not in _UPGRADED:
        run_migrations(db_path)
        _UPGRADED.add(key)

OPEN, FULFILLED, CANCELLED = "open", "fulfilled", "cancelled"
NO_DATE = "no date given"  # what an undated promise stores: the table requires some due information, and the brief already says this
INGESTED, NUDGED, OVERDUE, ESCALATED = "ingested", "nudged", "overdue", "escalated"


class TrackedCommitment(BaseModel):
    id: int
    member_id: str
    item_id: str | None = None
    text: str
    due_date_iso: str | None = None
    due_date_text: str | None = None
    made_at: str
    source_message_id: str | None = None
    status: str = OPEN
    closed_at: str | None = None
    source: str = "seed"


def _row(row: sqlite3.Row) -> TrackedCommitment:
    return TrackedCommitment(**dict(zip(row.keys(), tuple(row), strict=True)))


class CommitmentNotFoundError(KeyError):
    pass


class CommitmentTracker:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        ensure_current(db_path)
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return get_connection(self._db_path)

    def list(self, *, status: str | None = None, member_id: str | None = None) -> list[TrackedCommitment]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if member_id is not None:
            clauses.append("member_id = ?")
            params.append(member_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            return [_row(r) for r in conn.execute(f"SELECT * FROM commitments {where} ORDER BY id", params).fetchall()]
        finally:
            conn.close()

    def get(self, commitment_id: int) -> TrackedCommitment:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise CommitmentNotFoundError(commitment_id)
        return _row(row)

    def add(
        self, *, member_id: str, text: str, made_at: str, item_id: str | None = None, due_date_iso: str | None = None,
        due_date_text: str | None = None, source_message_id: str | None = None, source: str = "outcome",
    ) -> tuple[TrackedCommitment, bool]:
        """(commitment, created). The same statement from the same message is never added twice. A promise with
        no day in it is stored with the phrase NO_DATE: it is never held to a date it did not give."""
        if not due_date_iso and not due_date_text:
            due_date_text = NO_DATE
        conn = self._connect()
        try:
            if source_message_id is not None:
                existing = conn.execute("SELECT * FROM commitments WHERE source_message_id = ? AND text = ?",
                                        (source_message_id, text)).fetchone()
                if existing is not None:
                    return _row(existing), False
            cursor = conn.execute(
                "INSERT INTO commitments (member_id, item_id, text, due_date_iso, due_date_text, made_at, source_message_id, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (member_id, item_id, text, due_date_iso, due_date_text, made_at, source_message_id, source),
            )
            conn.commit()
            new_id = cursor.lastrowid
        finally:
            conn.close()
        return self.get(new_id), True

    def close(self, commitment_id: int, *, status: str, day: str, detail: str = "") -> bool:
        """Mark an open commitment fulfilled or cancelled. False if it was not open (nothing changes)."""
        if status not in (FULFILLED, CANCELLED):
            raise ValueError(f"a commitment is closed as fulfilled or cancelled, not {status!r}")
        conn = self._connect()
        try:
            cursor = conn.execute("UPDATE commitments SET status = ?, closed_at = ? WHERE id = ? AND status = ?",
                                  (status, day, commitment_id, OPEN))
            conn.commit()
            changed = cursor.rowcount == 1
        finally:
            conn.close()
        if changed:
            self.record_event(commitment_id, status, day, detail)
        return changed

    # --- events: each kind happens once per commitment -------------------------------------------------------------

    def record_event(self, commitment_id: int, kind: str, day: str, detail: str = "", proposal_id: str | None = None) -> bool:
        """True if recorded now; False if this kind of event had already happened for this commitment."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO commitment_events (commitment_id, kind, day, detail, proposal_id) VALUES (?, ?, ?, ?, ?)",
                (commitment_id, kind, day, detail, proposal_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def event(self, commitment_id: int, kind: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM commitment_events WHERE commitment_id = ? AND kind = ?", (commitment_id, kind)).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    def events(self, commitment_id: int) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM commitment_events WHERE commitment_id = ? ORDER BY id", (commitment_id,))]
        finally:
            conn.close()

    def ever_escalated_about(self, member_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM commitment_events e JOIN commitments c ON c.id = e.commitment_id "
                "WHERE c.member_id = ? AND e.kind = ? LIMIT 1", (member_id, ESCALATED)).fetchone()
        finally:
            conn.close()
        return row is not None
