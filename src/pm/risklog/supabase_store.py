"""The delivery-lead-facing risk log: a real, editable table in Postgres (Supabase).

This is the stand-in for the Dataverse table the plan names, which this account cannot
create. The lead opens `p2.risk_log` in the Supabase Table Editor and edits it like a
spreadsheet. It has real columns and CHECK constraints, so a bad edit (a severity of
"urgent") is refused by the database itself as well as by the sync.

It lives in its own schema ("p2" by default, never the shared "public"), every
identifier is quoted, every value is a bound parameter, writes are one transaction,
and the connection string is a credential that is never put in an error message.
Swapping in a real Dataverse table later means writing one class with these same
methods; nothing else changes.
"""

from __future__ import annotations

import re

from pm.adapters.risk_log import (
    DuplicateRiskError,
    Risk,
    RiskLogStore,
    RiskNotFoundError,
)
from pm.mirror.supabase_mirror import _scrub
from pm.risklog.csv_store import SEVERITIES, STATUSES
from pm.risklog.remote import RemoteUnavailableError

TABLE = "risk_log"
_SCHEMA_OK = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_COLUMNS = ["id", "title", "description", "severity", "status", "related_item_id", "opened_at", "owner"]
_PLACEHOLDERS = ", ".join(["%s"] * len(_COLUMNS))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v + "'" for v in values)


class SupabaseRiskLog(RiskLogStore):
    def __init__(self, db_url: str, *, schema: str = "p2", connect=None) -> None:
        if not _SCHEMA_OK.match(schema) or schema in ("public", "information_schema") or schema.startswith("pg_"):
            raise ValueError(f"refusing schema {schema!r}: use a private schema name such as 'p2'")
        self._url = db_url
        self._schema = schema
        self._target = f"{_q(schema)}.{_q(TABLE)}"
        self._connect = connect

    # --- plumbing -----------------------------------------------------------------------------------------

    def _open(self):
        try:
            if self._connect is not None:
                return self._connect(self._url)
            import psycopg2

            return psycopg2.connect(self._url, connect_timeout=10)
        except Exception as exc:  # noqa: BLE001 - reported without the credential
            raise RemoteUnavailableError(
                _scrub(f"could not connect to the lead-facing risk log: {type(exc).__name__}: {exc}", self._url)
            ) from None

    def _run(self, work):
        """Open a connection, make sure the table exists, run `work(cursor)`, commit
        once. Any failure rolls everything back and is reported without the URL."""
        conn = self._open()
        try:
            try:
                with conn.cursor() as cur:
                    self._ddl(cur)
                    result = work(cur)
                conn.commit()
                return result
            except (DuplicateRiskError, RiskNotFoundError):
                conn.rollback()
                raise
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                raise RemoteUnavailableError(
                    _scrub(f"the lead-facing risk log failed and was rolled back: {type(exc).__name__}: {exc}", self._url)
                ) from None
        finally:
            conn.close()

    def _ddl(self, cur) -> None:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(self._schema)}")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {self._target} ("
            '"id" text PRIMARY KEY, '
            '"title" text NOT NULL, '
            "\"description\" text NOT NULL DEFAULT '', "
            f'"severity" text NOT NULL CHECK ("severity" IN ({_sql_list(SEVERITIES)})), '
            f'"status" text NOT NULL CHECK ("status" IN ({_sql_list(STATUSES)})), '
            '"related_item_id" text, '
            '"opened_at" date NOT NULL, '
            '"owner" text)'
        )
        # a table made before the owner column existed gets it, once, additively: nothing is dropped or rewritten
        cur.execute(f'ALTER TABLE {self._target} ADD COLUMN IF NOT EXISTS "owner" text')

    def ensure_table(self) -> None:
        self._run(lambda cur: None)

    @staticmethod
    def _risk(row) -> Risk:
        opened = row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6])
        return Risk(id=row[0], title=row[1], description=row[2] or "", severity=row[3], status=row[4],
                    related_item_id=row[5], opened_at=opened, owner=row[7] or None)

    @staticmethod
    def _values(risk: Risk) -> tuple:
        return (risk.id, risk.title, risk.description, risk.severity, risk.status, risk.related_item_id, risk.opened_at, risk.owner)

    # --- the store interface ----------------------------------------------------------------------------------

    def list_risks(self) -> list[Risk]:
        def work(cur):
            cur.execute(f"SELECT {', '.join(_q(c) for c in _COLUMNS)} FROM {self._target} ORDER BY \"id\"")
            return [self._risk(row) for row in cur.fetchall()]

        return self._run(work)

    def get_risk(self, risk_id: str) -> Risk:
        def work(cur):
            cur.execute(f"SELECT {', '.join(_q(c) for c in _COLUMNS)} FROM {self._target} WHERE \"id\" = %s", (risk_id,))
            row = cur.fetchone()
            if row is None:
                raise RiskNotFoundError(risk_id)
            return self._risk(row)

        return self._run(work)

    def create_risk(self, payload: Risk) -> Risk:
        def work(cur):
            cur.execute(
                f"INSERT INTO {self._target} ({', '.join(_q(c) for c in _COLUMNS)}) VALUES ({_PLACEHOLDERS}) "
                'ON CONFLICT ("id") DO NOTHING',
                self._values(payload),
            )
            if cur.rowcount == 0:
                raise DuplicateRiskError(payload.id)
            return payload

        return self._run(work)

    def update_risk(self, risk_id: str, payload: Risk) -> Risk:
        def work(cur):
            cur.execute(
                f'UPDATE {self._target} SET "title" = %s, "description" = %s, "severity" = %s, "status" = %s, '
                '"related_item_id" = %s, "opened_at" = %s, "owner" = %s WHERE "id" = %s',
                (payload.title, payload.description, payload.severity, payload.status,
                 payload.related_item_id, payload.opened_at, payload.owner, risk_id),
            )
            if cur.rowcount == 0:
                raise RiskNotFoundError(risk_id)
            return payload

        return self._run(work)

    def replace_all(self, risks: list[Risk]) -> None:
        """Make the table exactly `risks`: upsert each, delete the rest, all in one
        transaction."""
        def work(cur):
            if risks:
                updates = ", ".join(f"{_q(c)} = EXCLUDED.{_q(c)}" for c in _COLUMNS if c != "id")
                cur.executemany(
                    f"INSERT INTO {self._target} ({', '.join(_q(c) for c in _COLUMNS)}) VALUES ({_PLACEHOLDERS}) "
                    f'ON CONFLICT ("id") DO UPDATE SET {updates}',
                    [self._values(r) for r in risks],
                )
            cur.execute(f'DELETE FROM {self._target} WHERE NOT ("id" = ANY(%s))', ([r.id for r in risks],))

        self._run(work)
