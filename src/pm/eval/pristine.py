"""A pristine seeded database for golden cases.

Golden cases must give the same numbers on every run, whatever is in the developer's
data/pm.db (pending proposals, a lead's edits to the live risk log). They build their
own throwaway database from the frozen seed instead of opening that one.
"""

from __future__ import annotations

from pathlib import Path

from spine.storage.db import run_migrations

from pm.seed.build import RISKS, build_seed
from pm.storage.db import MIGRATIONS_DIR, get_connection


def build_pristine_database(directory: Path) -> Path:
    """Migrate and seed a fresh database in `directory` from the frozen seed (the
    original three risks, not the live risk log)."""
    db_path = directory / "pristine.db"
    run_migrations(db_path, MIGRATIONS_DIR)
    conn = get_connection(db_path)
    try:
        build_seed(conn, risks=RISKS)
    finally:
        conn.close()
    return db_path
