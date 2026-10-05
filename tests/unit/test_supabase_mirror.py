"""The P2 -> Supabase mirror: a one-way, read-only copy of data/pm.db into Postgres
so a lead can look at the tables in a browser. SQLite stays the source of truth;
nothing reads the mirror back.

It writes ONLY into its own Postgres schema ("p2" by default), so it can never mix
with or overwrite the P1 mirror's tables (which live in the default schema under
names like "proposals" and "audit"). The connection string is a credential and is
never logged or shown. A failed mirror never affects the app.

The SQL is tested against a recording fake connection; one test also runs against
a real Postgres when PM_TEST_POSTGRES_URL is set.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

import pytest

from pm.mirror import supabase_mirror
from pm.mirror.supabase_mirror import MirrorError, mirror_if_enabled, sync, tables_in

SECRET_URL = "postgresql://postgres:s3cr3t-pass@db.example.invalid:5432/postgres"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _record(self, sql, params):
        statement = " ".join(sql.split())
        if self.conn._fail_on and statement.startswith(self.conn._fail_on):
            raise RuntimeError(f"boom while talking to {SECRET_URL}")
        self.conn.statements.append((statement, params))

    def execute(self, sql, params=None):
        self._record(sql, params)

    def executemany(self, sql, rows):
        for row in rows:
            self._record(sql, row)


class FakeConn:
    def __init__(self, fail_on=None):
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._fail_on = fail_on

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def sql(self):
        return [s for s, _ in self.statements]


@pytest.fixture()
def small_db(tmp_path):
    path = tmp_path / "small.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE items (id TEXT PRIMARY KEY, title TEXT, n INTEGER);
        CREATE TABLE proposals (id TEXT PRIMARY KEY, payload TEXT, approver_id TEXT);
        CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT);
        CREATE TABLE empty_one (id TEXT PRIMARY KEY, note TEXT);
        INSERT INTO items VALUES ('PM-001', 'Ship "login" page', 3), ('PM-002', NULL, 4);
        INSERT INTO proposals VALUES ('abc', '{"content": "hi"}', NULL);
        INSERT INTO audit (actor, action) VALUES ('agent', 'proposal.created');
        """
    )
    conn.commit()
    conn.close()
    return path


# --- what gets mirrored -------------------------------------------------------------------


def test_every_real_table_with_a_primary_key_is_mirrored_and_sqlite_internals_are_not(small_db):
    assert dict(tables_in(small_db)) == {"items": "id", "proposals": "id", "audit": "id", "empty_one": "id"}


def test_the_real_p2_database_is_fully_covered(seeded_db_path):
    tables = dict(tables_in(seeded_db_path))

    for needed in ("proposals", "audit", "write_log", "snapshots", "items", "assignees", "commits", "schema_migrations"):
        assert needed in tables, needed
    assert not any(t.startswith("sqlite_") for t in tables)


# --- the SQL ----------------------------------------------------------------------------------


def test_everything_goes_into_the_p2_schema_never_the_default_one(small_db):
    conn = FakeConn()

    sync(small_db, SECRET_URL, connect=lambda url: conn)

    statements = conn.sql()
    assert statements[0] == 'CREATE SCHEMA IF NOT EXISTS "p2"'
    for statement in statements[1:]:
        assert '"p2".' in statement, statement  # every table touched is schema-qualified
    assert not any(s.startswith("DROP") for s in statements)


def test_tables_are_created_with_text_columns_and_the_real_primary_key(small_db):
    conn = FakeConn()

    sync(small_db, SECRET_URL, connect=lambda url: conn)

    create = next(s for s in conn.sql() if s.startswith('CREATE TABLE IF NOT EXISTS "p2"."items"'))
    assert '"id" TEXT' in create and '"title" TEXT' in create and '"n" TEXT' in create
    assert 'PRIMARY KEY ("id")' in create


def test_rows_are_upserted_with_values_as_text_and_nulls_kept(small_db):
    conn = FakeConn()

    sync(small_db, SECRET_URL, connect=lambda url: conn)

    upsert = next(s for s in conn.sql() if s.startswith('INSERT INTO "p2"."items"'))
    assert 'ON CONFLICT ("id") DO UPDATE SET' in upsert
    rows = [p for s, p in conn.statements if s.startswith('INSERT INTO "p2"."items"')]
    assert ("PM-001", 'Ship "login" page', "3") in rows and ("PM-002", None, "4") in rows


def test_identifiers_are_quoted_so_odd_names_cannot_break_out(tmp_path):
    path = tmp_path / "odd.db"
    sqlite3.connect(path).executescript('CREATE TABLE "we""ird" (id TEXT PRIMARY KEY, "col umn" TEXT); INSERT INTO "we""ird" VALUES (\'1\', \'x\');')
    conn = FakeConn()

    sync(path, SECRET_URL, connect=lambda url: conn)

    assert any('"p2"."we""ird"' in s and '"col umn"' in s for s in conn.sql())


def test_rows_deleted_from_sqlite_are_removed_from_the_mirror(small_db):
    conn = FakeConn()

    sync(small_db, SECRET_URL, connect=lambda url: conn)

    prunes = [(s, p) for s, p in conn.statements if s.startswith('DELETE FROM "p2"."items"')]
    assert len(prunes) == 1 and sorted(prunes[0][1][0]) == ["PM-001", "PM-002"]
    empty = [(s, p) for s, p in conn.statements if s.startswith('DELETE FROM "p2"."empty_one"')]
    assert empty and empty[0][1][0] == []  # an emptied table is emptied in the mirror too


def test_a_new_column_in_sqlite_is_added_to_the_mirror(small_db):
    conn = FakeConn()

    sync(small_db, SECRET_URL, connect=lambda url: conn)

    assert any(s.startswith('ALTER TABLE "p2"."items" ADD COLUMN IF NOT EXISTS "title" TEXT') for s in conn.sql())


def test_it_all_happens_in_one_transaction_and_reports_counts(small_db):
    conn = FakeConn()

    result = sync(small_db, SECRET_URL, connect=lambda url: conn)

    assert conn.commits == 1 and conn.rollbacks == 0 and conn.closed
    assert result.rows == {"items": 2, "proposals": 1, "audit": 1, "empty_one": 0}


def test_the_sqlite_file_is_only_read(small_db):
    before = small_db.read_bytes()

    sync(small_db, SECRET_URL, connect=lambda url: FakeConn())

    assert small_db.read_bytes() == before


def test_a_custom_schema_is_used_and_validated(small_db):
    conn = FakeConn()
    sync(small_db, SECRET_URL, schema="p2_demo", connect=lambda url: conn)
    assert conn.sql()[0] == 'CREATE SCHEMA IF NOT EXISTS "p2_demo"'

    with pytest.raises(MirrorError, match="schema"):
        sync(small_db, SECRET_URL, schema="public", connect=lambda url: FakeConn())  # never the shared default schema
    with pytest.raises(MirrorError, match="schema"):
        sync(small_db, SECRET_URL, schema='x"; DROP SCHEMA p2; --', connect=lambda url: FakeConn())


# --- failure ---------------------------------------------------------------------------------------


def test_a_failure_rolls_back_and_never_leaks_the_connection_string(small_db):
    conn = FakeConn(fail_on='INSERT INTO "p2"."items"')

    with pytest.raises(MirrorError) as excinfo:
        sync(small_db, SECRET_URL, connect=lambda url: conn)

    assert conn.rollbacks == 1 and conn.commits == 0 and conn.closed
    assert "s3cr3t-pass" not in str(excinfo.value) and SECRET_URL not in str(excinfo.value)


def test_a_connection_failure_is_a_clean_error_without_the_password(small_db):
    def refuse(url):
        raise ConnectionError(f"could not connect to {url}")

    with pytest.raises(MirrorError) as excinfo:
        sync(small_db, SECRET_URL, connect=refuse)

    assert "s3cr3t-pass" not in str(excinfo.value)


def test_a_missing_sqlite_file_is_an_error_and_is_not_created(tmp_path):
    with pytest.raises(MirrorError, match="not found"):
        sync(tmp_path / "nope.db", SECRET_URL, connect=lambda url: FakeConn())

    assert not (tmp_path / "nope.db").exists()


@pytest.fixture(autouse=True)
def _small_db_is_the_configured_database(request, monkeypatch):
    """Most tests below exercise the automatic refresh on small_db, which only ever
    refreshes the configured database; make it that one (tests of the guard itself
    opt out by name)."""
    if "small_db" in request.fixturenames and "guard" not in request.node.name:
        monkeypatch.setenv("PM_DB_PATH", str(request.getfixturevalue("small_db")))


# --- the automatic refresh ----------------------------------------------------------------------------


@pytest.fixture()
def spy(monkeypatch, small_db):
    calls = []
    monkeypatch.setattr(supabase_mirror, "sync", lambda path, url, **kw: calls.append((str(path), url)) or None)
    monkeypatch.setattr(supabase_mirror, "_run_in_background", lambda fn: fn())  # run inline for the test
    return calls


def test_the_automatic_refresh_is_off_by_default(spy, small_db, monkeypatch):
    monkeypatch.delenv("PM_SUPABASE_MIRROR", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)

    mirror_if_enabled(small_db)

    assert spy == []


def test_it_needs_both_the_switch_exactly_on_and_a_url(spy, small_db, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)
    for value in ("0", "yes", "true", ""):
        monkeypatch.setenv("PM_SUPABASE_MIRROR", value)
        mirror_if_enabled(small_db)
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "1")
    monkeypatch.delenv("SUPABASE_DB_URL")
    mirror_if_enabled(small_db)

    assert spy == []

    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)
    mirror_if_enabled(small_db)
    assert spy == [(str(small_db), SECRET_URL)]


def test_a_failing_mirror_never_raises_into_the_app_and_logs_no_secret(spy, small_db, monkeypatch, caplog):
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "1")
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)

    def boom(path, url, **kw):
        raise MirrorError(f"could not reach {url}")

    monkeypatch.setattr(supabase_mirror, "sync", boom)

    with caplog.at_level(logging.WARNING):
        mirror_if_enabled(small_db)  # must not raise

    assert "s3cr3t-pass" not in caplog.text


def test_the_refresh_runs_in_the_background_so_the_app_does_not_wait(small_db, monkeypatch):
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "1")
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)
    started = []

    def slow(path, url, **kw):
        started.append(time.time())
        time.sleep(0.4)

    monkeypatch.setattr(supabase_mirror, "sync", slow)

    begin = time.time()
    mirror_if_enabled(small_db)
    elapsed = time.time() - begin
    supabase_mirror.wait_for_pending(timeout=5)

    assert elapsed < 0.2 and started  # it returned at once; the sync ran afterwards


def test_a_short_lived_command_waits_for_its_sync_before_exiting(small_db, monkeypatch):
    """A CLI run (--once, approve.py) ends right after its last write; the
    background sync must not be cut off with the process."""
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "1")
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)
    finished = []

    def slow(path, url, **kw):
        time.sleep(0.3)
        finished.append(True)

    monkeypatch.setattr(supabase_mirror, "sync", slow)

    mirror_if_enabled(small_db)
    assert finished == []  # still running in the background
    supabase_mirror._wait_at_exit()  # what the interpreter runs as the process ends

    assert finished == [True]


def test_the_exit_wait_is_registered_once(monkeypatch):
    registered = []
    monkeypatch.setattr(supabase_mirror.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(supabase_mirror, "_atexit_registered", False)

    supabase_mirror._run_in_background(lambda: None)
    supabase_mirror._run_in_background(lambda: None)
    supabase_mirror.wait_for_pending(timeout=2)

    assert registered == [supabase_mirror._wait_at_exit]


def test_a_burst_of_changes_is_coalesced_not_run_in_parallel(small_db, monkeypatch):
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "1")
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)
    running, peak, count = [0], [0], [0]

    def slow(path, url, **kw):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        count[0] += 1
        time.sleep(0.15)
        running[0] -= 1

    monkeypatch.setattr(supabase_mirror, "sync", slow)

    for _ in range(6):
        mirror_if_enabled(small_db)
    supabase_mirror.wait_for_pending(timeout=5)

    assert peak[0] == 1 and 1 <= count[0] <= 3  # never two at once; six calls became at most a few syncs


# --- the writers call it -----------------------------------------------------------------------------------


def test_the_job_and_the_approval_service_refresh_the_mirror_after_they_write(seeded_db_path, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from datetime import time as dtime

    from p1.adapters.teams_publisher_mock import LogPublisher

    from pm.approval.service import ApprovalPolicy, approve_and_send
    from pm.eval.pm12_cases import ScriptedGateway
    from pm.jobs.morning_brief_job import run_morning_brief_job
    from pm.scheduling.config import ProjectScheduleConfig
    from pm.seed.build import CHANNEL_ID

    seen = []
    monkeypatch.setattr("pm.mirror.hook.mirror_if_enabled", lambda path: seen.append(str(path)))
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=dtime(8, 0), end_of_day_time=dtime(17, 0),
    )
    policy = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))

    result = run_morning_brief_job(config, ScriptedGateway(), moment=datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc),
                                   db_path=seeded_db_path, policy=policy)
    approve_and_send(result.proposal_id, approver_id="sharon.silva", publisher=LogPublisher(tmp_path / "l.jsonl"),
                     policy=policy, db_path=seeded_db_path)

    assert seen.count(str(seeded_db_path)) >= 2  # once after the job, once after the approval


# --- against a real Postgres, when one is provided -------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("PM_TEST_POSTGRES_URL"), reason="set PM_TEST_POSTGRES_URL to run against a real Postgres")
def test_against_a_real_postgres(small_db):
    import psycopg2

    url = os.environ["PM_TEST_POSTGRES_URL"]
    sync(small_db, url, schema="p2_pytest")
    conn = sqlite3.connect(small_db)
    conn.execute("DELETE FROM items WHERE id = 'PM-002'")
    conn.execute("UPDATE items SET title = 'Changed' WHERE id = 'PM-001'")
    conn.commit()
    conn.close()
    sync(small_db, url, schema="p2_pytest")

    pg = psycopg2.connect(url)
    try:
        with pg.cursor() as cur:
            cur.execute('SELECT id, title FROM "p2_pytest"."items" ORDER BY id')
            assert cur.fetchall() == [("PM-001", "Changed")]  # updated in place, deleted row pruned
            cur.execute('SELECT count(*) FROM "p2_pytest"."empty_one"')
            assert cur.fetchone()[0] == 0
            cur.execute('DROP SCHEMA "p2_pytest" CASCADE')
        pg.commit()
    finally:
        pg.close()


# --- it can never touch anything but the real database ------------------------------------------------


def test_the_automatic_refresh_ignores_any_database_but_the_configured_one(small_db, tmp_path, monkeypatch):
    """The guard: a leaked mirror setting must never push a temporary or test
    database over the real mirror."""
    calls = []
    monkeypatch.setattr(supabase_mirror, "sync", lambda path, url, **kw: calls.append(str(path)))
    monkeypatch.setattr(supabase_mirror, "_run_in_background", lambda fn: fn())
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "1")
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)
    real = tmp_path / "real.db"
    real.write_bytes(small_db.read_bytes())
    monkeypatch.setenv("PM_DB_PATH", str(real))

    mirror_if_enabled(small_db)  # some other database, e.g. a test's temp file
    assert calls == []

    mirror_if_enabled(real)  # the configured one
    assert calls == [str(real)]


def test_the_guard_compares_real_paths_not_spellings(small_db, monkeypatch):
    calls = []
    monkeypatch.setattr(supabase_mirror, "sync", lambda path, url, **kw: calls.append(str(path)))
    monkeypatch.setattr(supabase_mirror, "_run_in_background", lambda fn: fn())
    monkeypatch.setenv("PM_SUPABASE_MIRROR", "1")
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)
    link = small_db.parent / "alias.db"
    link.symlink_to(small_db)  # a different spelling of the very same file
    monkeypatch.setenv("PM_DB_PATH", str(link))

    mirror_if_enabled(small_db)

    assert calls == [str(small_db)]


def test_the_test_suite_can_never_reach_the_real_mirror_even_after_loading_the_real_env_file():
    """The dashboard tests run the app, which calls load_dotenv() on the real .env.
    The suite pins the mirror off, and load_dotenv does not override what is set."""
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    assert os.environ.get("PM_SUPABASE_MIRROR") == "0"
    assert os.environ.get("SUPABASE_DB_URL") == ""
