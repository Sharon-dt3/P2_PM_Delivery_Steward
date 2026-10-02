"""
SQLite schema and migrations (SPN-04).

The schema lives entirely in the committed .sql files under
storage/migrations/, applied in filename order. Nothing here creates a
table ad hoc -- every table exists because a migration file says so.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("spine.storage.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_DB_PATH = Path("data/p1.db")


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def run_migrations(
    db_path: str | Path = DEFAULT_DB_PATH,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply every migration not yet recorded as applied, in filename order.
    A fresh DB is fully built by calling this alone. Returns the filenames
    applied this run (empty list if already up to date)."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        applied = {row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")}

        newly_applied = []
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in applied:
                continue
            logger.info("Applying migration %s", path.name)
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,))
            conn.commit()
            newly_applied.append(path.name)

        return newly_applied
    finally:
        conn.close()


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Seed-command entry point: build a fresh DB from migrations alone."""
    applied = run_migrations(db_path)
    if applied:
        logger.info("Database initialised at %s (%d migration(s) applied)", db_path, len(applied))
    else:
        logger.info("Database at %s already up to date", db_path)
