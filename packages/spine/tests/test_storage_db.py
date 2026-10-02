"""
Real proof of spine.storage.db's migration engine against a throwaway
migrations directory that has nothing to do with P1's own schema.
"""
from __future__ import annotations

from pathlib import Path

from spine.storage.db import get_connection, run_migrations


def test_run_migrations_applies_in_filename_order_and_is_idempotent(tmp_path: Path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_widgets.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY, name TEXT NOT NULL);"
    )
    (migrations_dir / "0002_seed.sql").write_text(
        "INSERT INTO widgets (id, name) VALUES ('w1', 'first widget');"
    )
    db_path = tmp_path / "test.db"

    applied = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied == ["0001_widgets.sql", "0002_seed.sql"]

    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT id, name FROM widgets").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["name"] == "first widget"

    # re-running applies nothing new -- already recorded in schema_migrations
    applied_again = run_migrations(db_path, migrations_dir=migrations_dir)
    assert applied_again == []


def test_get_connection_creates_parent_directory(tmp_path: Path):
    db_path = tmp_path / "nested" / "dir" / "test.db"
    conn = get_connection(db_path)
    conn.close()
    assert db_path.parent.is_dir()
