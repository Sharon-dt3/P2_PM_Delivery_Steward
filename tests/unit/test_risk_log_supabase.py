"""PM-15: the delivery-lead-facing risk log, as a real editable table in Postgres
(Supabase) -- the stand-in for the Dataverse table the plan names, which this
account cannot create.

The lead opens `p2.risk_log` in the Table Editor and edits it. The table has real
columns and CHECK constraints, so a bad edit is refused by the database itself, not
only by us. Everything is schema-qualified (never the shared default schema), every
value is a bound parameter, and the connection string is never shown.

SQL is tested against a recording fake; one test round-trips against a real
Postgres when PM_TEST_POSTGRES_URL is set.
"""

from __future__ import annotations

import datetime
import os

import pytest

from pm.adapters.risk_log import DuplicateRiskError, Risk, RiskNotFoundError
from pm.risklog.remote import RemoteUnavailableError
from pm.risklog.supabase_store import TABLE, SupabaseRiskLog

SECRET_URL = "postgresql://postgres:s3cr3t-pass@db.example.invalid:5432/postgres"


def _risk(**changes):
    values = {"id": "RISK-010", "title": "T", "description": "D", "severity": "low", "status": "open",
              "related_item_id": None, "opened_at": "2026-09-20"}
    return Risk(**{**values, **changes})


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        if self.conn.fail_on and statement.startswith(self.conn.fail_on):
            raise RuntimeError(f"boom talking to {SECRET_URL}")
        self.conn.statements.append((statement, params))
        self.rowcount = self.conn.rowcount

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def fetchall(self):
        return list(self.conn.rows)

    def fetchone(self):
        return self.conn.rows[0] if self.conn.rows else None


class FakeConn:
    def __init__(self, rows=(), fail_on=None, rowcount=1):
        self.statements = []
        self.rows = rows
        self.fail_on = fail_on
        self.rowcount = rowcount
        self.commits = self.rollbacks = 0
        self.closed = False

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


def _store(conn, schema="p2"):
    return SupabaseRiskLog(SECRET_URL, schema=schema, connect=lambda url: conn)


# --- the table ----------------------------------------------------------------------------------------------------------


def test_the_table_has_real_columns_and_constraints_the_lead_cannot_break():
    conn = FakeConn()

    _store(conn).ensure_table()

    ddl = next(s for s in conn.sql() if s.startswith(f'CREATE TABLE IF NOT EXISTS "p2"."{TABLE}"'))
    assert '"id" text PRIMARY KEY' in ddl and '"title" text NOT NULL' in ddl
    assert "CHECK (\"severity\" IN ('low', 'medium', 'high'))" in ddl
    assert "CHECK (\"status\" IN ('open', 'mitigated', 'closed'))" in ddl
    assert '"opened_at" date NOT NULL' in ddl and '"related_item_id" text' in ddl and '"owner" text' in ddl
    assert conn.sql()[0] == 'CREATE SCHEMA IF NOT EXISTS "p2"'


def test_a_table_made_before_the_owner_column_gets_it_additively_and_nothing_is_dropped():
    conn = FakeConn()

    _store(conn).ensure_table()

    alter = [s for s in conn.sql() if s.startswith("ALTER TABLE")]
    assert alter == [f'ALTER TABLE "p2"."{TABLE}" ADD COLUMN IF NOT EXISTS "owner" text']
    assert not [s for s in conn.sql() if "DROP" in s.upper() or "TRUNCATE" in s.upper()]


def test_it_only_ever_touches_its_own_schema_and_never_the_shared_one():
    with pytest.raises(ValueError, match="schema"):
        SupabaseRiskLog(SECRET_URL, schema="public")
    with pytest.raises(ValueError, match="schema"):
        SupabaseRiskLog(SECRET_URL, schema='p2"; DROP SCHEMA public; --')
    conn = FakeConn()
    _store(conn).replace_all([_risk()])
    assert all('"p2".' in s or s.startswith('CREATE SCHEMA IF NOT EXISTS "p2"') for s in conn.sql())


# --- reading ---------------------------------------------------------------------------------------------------------------


def test_list_returns_risks_with_dates_as_iso_text_in_id_order():
    conn = FakeConn(rows=[("RISK-001", "T", "D", "medium", "open", "PM-023", datetime.date(2026, 9, 10), "Wei Chen (wei.chen)"),
                          ("RISK-003", "T3", "", "low", "mitigated", None, datetime.date(2026, 8, 20), None)])

    risks = _store(conn).list_risks()

    assert [r.id for r in risks] == ["RISK-001", "RISK-003"]
    assert risks[0].opened_at == "2026-09-10" and risks[1].related_item_id is None and risks[1].description == ""
    assert risks[0].owner == "Wei Chen (wei.chen)" and risks[1].owner is None
    assert any(s.startswith('SELECT') and f'"p2"."{TABLE}"' in s and "ORDER BY" in s for s in conn.sql())


def test_get_risk_and_the_not_found_error():
    found = _store(FakeConn(rows=[("RISK-001", "T", "D", "medium", "open", "PM-023", datetime.date(2026, 9, 10), None)]))
    assert found.get_risk("RISK-001").id == "RISK-001"

    with pytest.raises(RiskNotFoundError):
        _store(FakeConn(rows=[])).get_risk("RISK-404")


# --- writing ----------------------------------------------------------------------------------------------------------------


def test_create_inserts_with_bound_values_and_refuses_a_duplicate():
    conn = FakeConn(rowcount=1)
    _store(conn).create_risk(_risk(related_item_id="PM-001", owner="Olivia Dupree (olivia.dupree)"))

    insert, params = next((s, p) for s, p in conn.statements if s.startswith(f'INSERT INTO "p2"."{TABLE}"'))
    assert "DO NOTHING" in insert and params == ("RISK-010", "T", "D", "low", "open", "PM-001", "2026-09-20", "Olivia Dupree (olivia.dupree)")

    with pytest.raises(DuplicateRiskError):
        _store(FakeConn(rowcount=0)).create_risk(_risk())  # DO NOTHING inserted no row: it already existed


def test_update_changes_one_row_and_refuses_an_unknown_one():
    conn = FakeConn(rowcount=1)
    _store(conn).update_risk("RISK-010", _risk(status="closed"))
    assert any(s.startswith(f'UPDATE "p2"."{TABLE}"') for s in conn.sql())

    with pytest.raises(RiskNotFoundError):
        _store(FakeConn(rowcount=0)).update_risk("RISK-404", _risk(id="RISK-404"))


def test_replace_all_upserts_deletes_the_rest_and_is_one_transaction():
    conn = FakeConn()

    _store(conn).replace_all([_risk(id="RISK-001"), _risk(id="RISK-002")])

    sql = conn.sql()
    assert sum(1 for s in sql if s.startswith(f'INSERT INTO "p2"."{TABLE}"')) == 2
    assert any("ON CONFLICT (\"id\") DO UPDATE SET" in s for s in sql)
    delete = next((s, p) for s, p in conn.statements if s.startswith(f'DELETE FROM "p2"."{TABLE}"'))
    assert sorted(delete[1][0]) == ["RISK-001", "RISK-002"]
    assert conn.commits == 1 and conn.rollbacks == 0 and conn.closed


def test_replace_all_with_nothing_empties_the_table():
    conn = FakeConn()

    _store(conn).replace_all([])

    delete = next((s, p) for s, p in conn.statements if s.startswith("DELETE"))
    assert delete[1][0] == []


# --- failure ----------------------------------------------------------------------------------------------------------------


def test_a_failed_write_rolls_back_and_never_leaks_the_connection_string():
    conn = FakeConn(fail_on=f'INSERT INTO "p2"."{TABLE}"')

    with pytest.raises(RemoteUnavailableError) as excinfo:
        _store(conn).replace_all([_risk()])

    assert conn.rollbacks == 1 and conn.commits == 0 and conn.closed
    assert "s3cr3t-pass" not in str(excinfo.value) and SECRET_URL not in str(excinfo.value)


def test_a_connection_failure_is_a_clean_error_without_the_password():
    def refuse(url):
        raise ConnectionError(f"could not connect to {url}")

    with pytest.raises(RemoteUnavailableError) as excinfo:
        SupabaseRiskLog(SECRET_URL, connect=refuse).list_risks()

    assert "s3cr3t-pass" not in str(excinfo.value)


# --- against a real Postgres, when one is provided -----------------------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("PM_TEST_POSTGRES_URL"), reason="set PM_TEST_POSTGRES_URL to run against a real Postgres")
def test_against_a_real_postgres():
    import psycopg2

    url = os.environ["PM_TEST_POSTGRES_URL"]
    store = SupabaseRiskLog(url, schema="p2_riskpytest")
    risks = [_risk(id="RISK-001", related_item_id="PM-023"), _risk(id="RISK-002", severity="high")]
    try:
        store.replace_all(risks)
        assert store.list_risks() == risks

        store.update_risk("RISK-002", _risk(id="RISK-002", status="closed"))
        assert store.get_risk("RISK-002").status == "closed"
        with pytest.raises(DuplicateRiskError):
            store.create_risk(_risk(id="RISK-001"))

        store.replace_all([_risk(id="RISK-009")])
        assert [r.id for r in store.list_risks()] == ["RISK-009"]  # the others were removed

        pg = psycopg2.connect(url)
        try:
            with pg.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute('INSERT INTO "p2_riskpytest"."risk_log" VALUES (%s, %s, %s, %s, %s, %s, %s)',
                            ("RISK-777", "T", "D", "urgent", "open", None, "2026-09-20"))  # the lead's table refuses it
        finally:
            pg.rollback()
            pg.close()
    finally:
        pg = psycopg2.connect(url)
        with pg.cursor() as cur:
            cur.execute('DROP SCHEMA IF EXISTS "p2_riskpytest" CASCADE')
        pg.commit()
        pg.close()
