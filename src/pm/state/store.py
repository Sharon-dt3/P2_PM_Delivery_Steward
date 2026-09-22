"""
Snapshot persistence (PM-05).

Snapshots are stored one row per `taken_at` in the `snapshots` table (see
storage/migrations/0002_snapshots.sql), as an opaque JSON payload --
there is no second schema for this module to own beyond "when was this
taken": a ProjectSnapshot's own shape already belongs to the tracker/
code-host/teams-reader adapters' models (PM-04).

Opens and closes its own connection per call, the same posture every
other store in this repo takes (TrackerMock/CodeHostMock/RiskLogMock).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pm.state.snapshot import ProjectSnapshot
from pm.storage.db import DEFAULT_DB_PATH, get_connection


class SnapshotNotFoundError(Exception):
    """Raised by read_snapshot() for a taken_at with no matching row."""


class DuplicateSnapshotError(Exception):
    """Raised by save_snapshot() when a snapshot for that exact taken_at
    already exists. taken_at is expected to be a fresh value per run
    (build_snapshot() defaults it to "now"), not a key callers reuse."""


@contextmanager
def _conn(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def save_snapshot(snapshot: ProjectSnapshot, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Persists snapshot, keyed by its own taken_at. Raises
    DuplicateSnapshotError rather than silently overwriting an existing
    row for that taken_at -- a snapshot is a point-in-time record, not
    something later code should be able to quietly rewrite."""
    with _conn(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM snapshots WHERE taken_at = ?", (snapshot.taken_at,)).fetchone()
        if exists is not None:
            raise DuplicateSnapshotError(snapshot.taken_at)
        conn.execute(
            "INSERT INTO snapshots (taken_at, payload) VALUES (?, ?)",
            (snapshot.taken_at, snapshot.model_dump_json()),
        )
        conn.commit()


def read_snapshot(taken_at: str, db_path: str | Path = DEFAULT_DB_PATH) -> ProjectSnapshot:
    """Reads back the snapshot persisted under taken_at, independently
    of whatever save_snapshot() call wrote it -- this function only ever
    goes through the database, never a same-process cache, so it proves
    the row genuinely round-trips rather than returning an in-memory
    object handed back to itself."""
    with _conn(db_path) as conn:
        row = conn.execute("SELECT payload FROM snapshots WHERE taken_at = ?", (taken_at,)).fetchone()
    if row is None:
        raise SnapshotNotFoundError(taken_at)
    return ProjectSnapshot.model_validate_json(row["payload"])


def list_snapshot_timestamps(db_path: str | Path = DEFAULT_DB_PATH) -> list[str]:
    """Every taken_at on file, oldest first -- so a caller can find the
    latest snapshot and the one before it to diff against."""
    with _conn(db_path) as conn:
        rows = conn.execute("SELECT taken_at FROM snapshots ORDER BY taken_at").fetchall()
    return [row["taken_at"] for row in rows]
