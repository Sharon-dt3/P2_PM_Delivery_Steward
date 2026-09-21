"""
Risk-log adapter -- interface (PM-04).

Narrow interface for reading and maintaining the delivery risk log:
list, read one, file a new one, update an existing one. Agent logic
depends ONLY on this interface -- no storage detail may leak past it.

The master plan's own real-implementation target for this adapter is "a
Dataverse table for human-facing view plus committed JSON mirror as
system of record" -- not this D11 mock's job to build (no Dataverse
access exists yet; see the CHN-25 Dataverse admin-access request tracked
in P1). RiskLogMock's backing store here is this repo's own seeded
`risks` SQLite table (src/pm/seed/build.py), the same mock posture
TrackerMock/CodeHostMock take over their own tables; a real
implementation would sit behind this same interface later.

The master plan's own edge cases for this adapter, and where each one
is actually exercised in the seed:
  - three pre-existing entries          -> RISK-001/002/003
  - two matching current blockers        -> RISK-001 -> PM-023,
    RISK-002 -> PM-024 (RISK-003 matches none -- see build.py's own
    comment on why)
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel

from pm.storage.db import DEFAULT_DB_PATH, get_connection


class Risk(BaseModel):
    id: str
    title: str
    description: str
    severity: str  # low | medium | high
    status: str  # open | mitigated | closed
    related_item_id: str | None = None
    opened_at: str


class RiskNotFoundError(Exception):
    """Raised by get_risk()/update_risk() for an unknown risk id."""


class DuplicateRiskError(Exception):
    """Raised by create_risk() when payload.id already names an
    existing risk."""


class RiskLogStore(ABC):
    """Read/write access to the delivery risk log. A narrow interface,
    not a passthrough for Dataverse's own table shape (see this
    module's own docstring on the real implementation this stands in
    for)."""

    @abstractmethod
    def list_risks(self) -> list[Risk]: ...

    @abstractmethod
    def get_risk(self, risk_id: str) -> Risk:
        """Raises RiskNotFoundError if risk_id doesn't exist."""

    @abstractmethod
    def create_risk(self, payload: Risk) -> Risk:
        """Raises DuplicateRiskError if payload.id already exists."""

    @abstractmethod
    def update_risk(self, risk_id: str, payload: Risk) -> Risk:
        """Replaces risk_id's row with payload (payload.id must equal
        risk_id). Raises RiskNotFoundError if risk_id doesn't exist."""


def _row_to_risk(row: sqlite3.Row) -> Risk:
    return Risk(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        severity=row["severity"],
        status=row["status"],
        related_item_id=row["related_item_id"],
        opened_at=row["opened_at"],
    )


class RiskLogMock(RiskLogStore):
    """The only RiskLogStore implementation so far: a mock over this
    repo's own seeded `risks` table (PM-01/02). Opens and closes its
    own connection per call, same posture as TrackerMock/CodeHostMock."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = get_connection(self._db_path)
        try:
            yield conn
        finally:
            conn.close()

    def list_risks(self) -> list[Risk]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM risks ORDER BY id").fetchall()
        return [_row_to_risk(row) for row in rows]

    def get_risk(self, risk_id: str) -> Risk:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM risks WHERE id = ?", (risk_id,)).fetchone()
        if row is None:
            raise RiskNotFoundError(risk_id)
        return _row_to_risk(row)

    def create_risk(self, payload: Risk) -> Risk:
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM risks WHERE id = ?", (payload.id,)).fetchone()
            if exists is not None:
                raise DuplicateRiskError(payload.id)
            conn.execute(
                """
                INSERT INTO risks (id, title, description, severity, status, related_item_id, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.id,
                    payload.title,
                    payload.description,
                    payload.severity,
                    payload.status,
                    payload.related_item_id,
                    payload.opened_at,
                ),
            )
            conn.commit()
        return payload

    def update_risk(self, risk_id: str, payload: Risk) -> Risk:
        with self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM risks WHERE id = ?", (risk_id,)).fetchone()
            if exists is None:
                raise RiskNotFoundError(risk_id)
            conn.execute(
                """
                UPDATE risks SET title = ?, description = ?, severity = ?, status = ?, related_item_id = ?, opened_at = ?
                WHERE id = ?
                """,
                (
                    payload.title,
                    payload.description,
                    payload.severity,
                    payload.status,
                    payload.related_item_id,
                    payload.opened_at,
                    risk_id,
                ),
            )
            conn.commit()
            updated_row = conn.execute("SELECT * FROM risks WHERE id = ?", (risk_id,)).fetchone()
        return _row_to_risk(updated_row)
