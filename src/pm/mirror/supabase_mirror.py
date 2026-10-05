"""Mirror data/pm.db into Postgres (Supabase), purely so a person can look at the
tables in a browser. SQLite stays the system of record: nothing reads the mirror
back, and the SQLite file is only ever read here.

Safe by construction:
- everything is written into ONE Postgres schema ("p2" by default, never "public"),
  so it can never mix with, or overwrite, P1's mirror (which uses the default
  schema with names like "proposals" and "audit");
- every identifier is quoted, every value is a bound parameter;
- the whole sync is one transaction: all of it, or none of it;
- the connection string is a credential: it is never logged or put in an error;
- a failed sync raises MirrorError (a clean message) from sync(), and is only logged
  from mirror_if_enabled(), so a mirror problem can never affect the app.

Every column is stored as TEXT, as in P1's mirror: this is for reading, not for
querying or computing. Rows deleted in SQLite are deleted in the mirror, so it is a
true copy, not an ever-growing pile.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("pm.mirror")

DEFAULT_SCHEMA = "p2"
_SCHEMA_OK = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class MirrorError(Exception):
    """The mirror could not be written. The message never contains the connection string."""


@dataclass(frozen=True)
class MirrorResult:
    rows: dict[str, int]  # rows now in the mirror, by table

    @property
    def total(self) -> int:
        return sum(self.rows.values())


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _scrub(text: str, url: str) -> str:
    """Remove the connection string, and the password inside it, from any text."""
    cleaned = text.replace(url, "<connection string>")
    password = re.match(r"^[a-z+]+://[^:@/]+:([^@]+)@", url)
    if password and password.group(1):
        cleaned = cleaned.replace(password.group(1), "<password>")
    return cleaned


def tables_in(sqlite_path: str | Path) -> list[tuple[str, str]]:
    """(table, primary key column) for every real table that has a single-column
    primary key. SQLite's own internal tables are not project data."""
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        names = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out = []
        for name in names:
            keys = [row[1] for row in conn.execute(f"PRAGMA table_info({_q(name)})") if row[5] > 0]
            if len(keys) == 1:
                out.append((name, keys[0]))
        return out
    finally:
        conn.close()


def sync(sqlite_path: str | Path, db_url: str, *, schema: str = DEFAULT_SCHEMA, connect=None) -> MirrorResult:
    if not _SCHEMA_OK.match(schema) or schema in ("public", "information_schema") or schema.startswith("pg_"):
        raise MirrorError(f"refusing schema {schema!r}: use a private schema name such as {DEFAULT_SCHEMA!r}")
    path = Path(sqlite_path)
    if not path.exists():
        raise MirrorError(f"SQLite database not found at {path}")  # looking must never create it

    if connect is None:
        import psycopg2

        connect = lambda url: psycopg2.connect(url, connect_timeout=10)

    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    pg = None
    try:
        try:
            pg = connect(db_url)
        except Exception as exc:  # noqa: BLE001 - reported without the credential
            raise MirrorError(_scrub(f"could not connect to the mirror database: {type(exc).__name__}: {exc}", db_url)) from None
        counts: dict[str, int] = {}
        try:
            with pg.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")
                for table, pk in tables_in(path):
                    counts[table] = _sync_table(source, cur, schema, table, pk)
            pg.commit()
        except MirrorError:
            pg.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - one transaction: undo everything, report without the credential
            pg.rollback()
            raise MirrorError(_scrub(f"mirror failed and was rolled back: {type(exc).__name__}: {exc}", db_url)) from None
        return MirrorResult(rows=counts)
    finally:
        source.close()
        if pg is not None:
            pg.close()


def _sync_table(source: sqlite3.Connection, cur, schema: str, table: str, pk: str) -> int:
    columns = [row[1] for row in source.execute(f"PRAGMA table_info({_q(table)})")]
    target = f"{_q(schema)}.{_q(table)}"
    col_list = ", ".join(_q(c) for c in columns)

    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {target} ({', '.join(f'{_q(c)} TEXT' for c in columns)}, PRIMARY KEY ({_q(pk)}))"
    )
    for column in columns:  # a column added to SQLite later appears in the mirror too
        cur.execute(f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS {_q(column)} TEXT")

    rows = [
        tuple(None if v is None else str(v) for v in row)
        for row in source.execute(f"SELECT {col_list} FROM {_q(table)}")
    ]
    if rows:
        updates = [c for c in columns if c != pk]
        conflict = (
            f"ON CONFLICT ({_q(pk)}) DO UPDATE SET " + ", ".join(f"{_q(c)} = EXCLUDED.{_q(c)}" for c in updates)
            if updates else f"ON CONFLICT ({_q(pk)}) DO NOTHING"
        )
        cur.executemany(
            f"INSERT INTO {target} ({col_list}) VALUES ({', '.join(['%s'] * len(columns))}) {conflict}", rows
        )
    pk_index = columns.index(pk)
    cur.execute(f"DELETE FROM {target} WHERE NOT ({_q(pk)} = ANY(%s))", ([r[pk_index] for r in rows],))
    return len(rows)


# --- the automatic refresh ------------------------------------------------------------------------

_lock = threading.Lock()
_state = {"running": False, "dirty": False, "thread": None}


def _run_in_background(fn) -> None:
    thread = threading.Thread(target=fn, name="pm-mirror", daemon=True)
    _state["thread"] = thread
    thread.start()


def wait_for_pending(timeout: float = 30.0) -> None:
    """Block until any background refresh has finished (for tests and shutdown)."""
    thread = _state.get("thread")
    if thread is not None:
        thread.join(timeout)


def mirror_if_enabled(db_path: str | Path) -> None:
    """Refresh the mirror after the app has written to SQLite -- only when
    PM_SUPABASE_MIRROR is exactly "1" and SUPABASE_DB_URL is set. Returns at once
    (the sync runs in the background), never raises, and bursts of writes are
    coalesced: while a sync runs, further requests just mark it for one more run."""
    url = os.environ.get("SUPABASE_DB_URL", "")
    if os.environ.get("PM_SUPABASE_MIRROR", "") != "1" or not url:
        return
    schema = os.environ.get("PM_SUPABASE_SCHEMA", DEFAULT_SCHEMA)

    with _lock:
        if _state["running"]:
            _state["dirty"] = True
            return
        _state["running"] = True

    def work() -> None:
        while True:
            try:
                sync(db_path, url, schema=schema)
            except Exception as exc:  # noqa: BLE001 - a mirror problem must never reach the app
                logger.warning("supabase mirror failed: %s", _scrub(f"{type(exc).__name__}: {exc}", url))
            with _lock:
                if _state["dirty"]:
                    _state["dirty"] = False
                    continue
                _state["running"] = False
                return

    _run_in_background(work)
