from __future__ import annotations

import os
from pathlib import Path as _Path

# The live risk log (risk_log/risks.csv) is data the delivery lead edits; tests run against
# the frozen original three so a legitimate edit can never break the suite. This must be set
# before anything imports pm.
os.environ["PM_RISK_LOG_CSV"] = str(
    _Path(__file__).resolve().parents[2] / "src" / "pm" / "seed" / "fixtures" / "risk_log_seed.csv"
)

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from spine.storage.db import run_migrations

from pm.seed.build import build_seed
from pm.storage.db import MIGRATIONS_DIR, get_connection


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


@pytest.fixture(autouse=True)
def _tests_never_touch_the_real_supabase_mirror(monkeypatch):
    """The real .env turns the Supabase mirror on and holds its URL. The dashboard
    tests run the app, which calls load_dotenv() on that file, and a leaked setting
    once pushed a temporary test database over the real mirror. Pin it off for every
    test: load_dotenv() does not override a variable that is already set (even to
    an empty value), and tests that need the mirror set their own values."""
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "0")
    monkeypatch.setenv("SUPABASE_DB_URL", "")
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "0")  # likewise: no test may rewrite the committed risk log
