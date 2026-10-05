#!/usr/bin/env python3
"""Copy data/pm.db into Postgres (Supabase) so it can be viewed in a browser.

One-way and read-only: SQLite stays the source of truth, nothing reads the copy
back, and this script only reads the SQLite file. Everything goes into its own
Postgres schema ("p2" by default), so it cannot mix with P1's mirror. Safe to run
repeatedly: rows are updated in place and rows deleted in SQLite are removed.

Needs SUPABASE_DB_URL in .env (the Direct connection URI from Supabase's Project
Settings -> Database, password percent-encoded). The URL is a credential and is
never printed.

To keep the copy fresh automatically, set PM_SUPABASE_MIRROR=1: the scheduler jobs
and every approval then refresh it in the background.

Usage:
    uv run python scripts/sync_to_supabase.py
    uv run python scripts/sync_to_supabase.py --dry-run     # list what would be copied
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.mirror.supabase_mirror import DEFAULT_SCHEMA, MirrorError, sync, tables_in
from pm.storage.db import DEFAULT_DB_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--schema", default=os.environ.get("PM_SUPABASE_SCHEMA", DEFAULT_SCHEMA))
    parser.add_argument("--dry-run", action="store_true", help="list the tables that would be copied and stop")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"SQLite database not found at {args.db}")
        return 1
    if args.dry_run:
        print(f"Would copy {args.db} into Postgres schema {args.schema!r}:")
        for table, pk in tables_in(args.db):
            print(f"  {table} (key: {pk})")
        return 0

    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("SUPABASE_DB_URL is not set (see .env.example). Nothing was copied.")
        return 1
    try:
        result = sync(args.db, url, schema=args.schema)
    except MirrorError as exc:
        print(f"Mirror failed: {exc}")
        return 1
    for table, count in result.rows.items():
        print(f"  {table}: {count} row(s)")
    print(f"Done: {result.total} row(s) in {len(result.rows)} table(s), schema {args.schema!r}. "
          "Open the Supabase Table Editor and choose that schema to see them.")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
