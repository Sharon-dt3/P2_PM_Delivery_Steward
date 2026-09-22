"""
Commitments adapter -- interface (PM-08's own dependency).

Narrow, read-only interface for reading who committed to what: PM-01/02
seeded this repo's own `commitments` table (one of D11's six artefact
rows) but no earlier task built an adapter over it -- everything through
PM-07 read it only via the seed itself or ad-hoc SQL. PM-08's "per person
committed / delivered / pending / blocked" facts is the first thing that
actually needs to read it back through code, so this is that adapter,
mirroring the same ABC-interface / pydantic-model / mock convention as
Tracker/CodeHost/RiskLogStore.

Read-only for now, the same posture CodeHost takes: nothing in this
agent's current scope files a new commitment or marks one fulfilled --
that would be a future task's job, sitting behind this same interface,
the same way a real tracker/code-host implementation would sit behind
Tracker/CodeHost later without callers changing.

A commitment's own "delivered" state is deliberately NOT modelled here:
the commitments table has no status column (see storage/migrations/
0001_initial.sql) -- whether a commitment was kept is derived by
cross-referencing its item_id against that item's tracker status
(PM-08's own job in pm/reporting/facts.py), not something this adapter
decides for its caller.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel

from pm.storage.db import DEFAULT_DB_PATH, get_connection


class Commitment(BaseModel):
    id: int
    member_id: str
    item_id: str | None = None
    text: str
    due_date_iso: str | None = None  # ISO date, when the commitment named one
    due_date_text: str | None = None  # raw relative phrase (e.g. "end of week"), when it didn't -- PM-03 edge case
    made_at: str
    source_message_id: str | None = None


class CommitmentNotFoundError(Exception):
    """Raised by get_commitment() for an unknown commitment id."""


class CommitmentsStore(ABC):
    """Read-only access to who committed to what. A narrow interface, not
    a passthrough for wherever these commitments actually get tracked
    (today: parsed out of channel messages into this repo's own seeded
    table; see this module's own docstring)."""

    @abstractmethod
    def list_commitments(self, member_id: str | None = None) -> list[Commitment]:
        """Every commitment on record, oldest first, optionally filtered
        to one member_id."""

    @abstractmethod
    def get_commitment(self, commitment_id: int) -> Commitment:
        """Raises CommitmentNotFoundError if commitment_id doesn't
        exist."""


def _row_to_commitment(row: sqlite3.Row) -> Commitment:
    return Commitment(
        id=row["id"],
        member_id=row["member_id"],
        item_id=row["item_id"],
        text=row["text"],
        due_date_iso=row["due_date_iso"],
        due_date_text=row["due_date_text"],
        made_at=row["made_at"],
        source_message_id=row["source_message_id"],
    )


class CommitmentsMock(CommitmentsStore):
    """The only CommitmentsStore implementation so far: a mock over this
    repo's own seeded `commitments` table (PM-01/02). Opens and closes
    its own connection per call, same posture as every other adapter
    mock in this repo."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = get_connection(self._db_path)
        try:
            yield conn
        finally:
            conn.close()

    def list_commitments(self, member_id: str | None = None) -> list[Commitment]:
        with self._conn() as conn:
            if member_id is not None:
                rows = conn.execute(
                    "SELECT * FROM commitments WHERE member_id = ? ORDER BY made_at, id", (member_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM commitments ORDER BY made_at, id").fetchall()
        return [_row_to_commitment(row) for row in rows]

    def get_commitment(self, commitment_id: int) -> Commitment:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
        if row is None:
            raise CommitmentNotFoundError(commitment_id)
        return _row_to_commitment(row)
