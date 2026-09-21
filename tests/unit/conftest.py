from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pm.seed.build import build_seed
from pm.storage.db import MIGRATIONS_DIR, get_connection
from spine.storage.db import run_migrations


def _build_seeded_db(db_path: Path) -> None:
    run_migrations(db_path, MIGRATIONS_DIR)
    conn = get_connection(db_path)
    try:
        build_seed(conn)
    finally:
        conn.close()


@pytest.fixture()
def seeded_conn(tmp_path) -> Iterator[sqlite3.Connection]:
    """A fresh, fully migrated and seeded database per test, as an open
    connection -- built the same way scripts/seed.py builds the real
    one, just against a tmp_path file so tests never touch data/pm.db."""
    db_path = tmp_path / "pm_test.db"
    _build_seeded_db(db_path)
    conn = get_connection(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def seeded_db_path(tmp_path) -> Path:
    """The same fresh, migrated and seeded database as seeded_conn, but
    as a file path rather than an open connection -- for the PM-04
    adapter mocks (TrackerMock/CodeHostMock/RiskLogMock), which each
    open and close their own connection per call rather than holding
    one open across calls."""
    db_path = tmp_path / "pm_adapter_test.db"
    _build_seeded_db(db_path)
    return db_path
