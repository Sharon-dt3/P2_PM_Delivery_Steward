"""
SQLite schema and migrations for P2 (PM Delivery Steward).

Reuses spine.storage.db's generic connection/migration engine verbatim
(get_connection, and the migration-running logic underneath
run_migrations) rather than reimplementing sqlite3 connection setup or a
migration runner here -- the same reuse P1 itself does in
../P3_Agents/src/p1/storage/db.py after CHN-33 (see that module's own
docstring). Only MIGRATIONS_DIR and DEFAULT_DB_PATH are redefined here,
because spine's own run_migrations()/DEFAULT_DB_PATH default bind to
spine's own (nonexistent) migrations directory and to a literal
"data/p1.db" path that would be misleading if reused verbatim in this
repo.
"""

from __future__ import annotations

import logging
from pathlib import Path

from spine.storage.db import get_connection
from spine.storage.db import run_migrations as _spine_run_migrations

logger = logging.getLogger("pm.storage.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_DB_PATH = Path("data/pm.db")

__all__ = ["DEFAULT_DB_PATH", "MIGRATIONS_DIR", "get_connection", "run_migrations", "init_db"]


def run_migrations(
    db_path: str | Path = DEFAULT_DB_PATH,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply every migration not yet recorded as applied, in filename
    order. Delegates to spine.storage.db's real engine, always passing
    this repo's own migrations_dir through explicitly."""
    return _spine_run_migrations(db_path, migrations_dir)


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Seed-command entry point: build a fresh DB from this repo's own
    migrations alone."""
    applied = run_migrations(db_path)
    if applied:
        logger.info("Database initialised at %s (%d migration(s) applied)", db_path, len(applied))
    else:
        logger.info("Database at %s already up to date", db_path)
