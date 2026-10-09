#!/usr/bin/env python3
"""PM-30: prove a weekly status report again, from the snapshots it was made from. Reads and changes nothing.

It reloads the three stored snapshots the report names, recomputes every figure, and checks that every number in the report's text is one of those figures and
that the quantities part of the report can be regenerated from the snapshots line for line. Exit code 0 means it all recomputes; 1 means something does not.

  --proposal ID   the weekly report's proposal id (the first characters are enough)
  --latest        the most recent weekly report
  --db PATH       the database (default data/pm.db)

Usage:
    uv run python scripts/verify_weekly_report.py --latest
    uv run python scripts/verify_weekly_report.py --proposal 172a8b8a
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.reporting.weekly_check import verify_stored_report
from pm.storage.db import DEFAULT_DB_PATH


def _find(db: str, prefix: str | None) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, status, json_extract(payload, '$.local_date') FROM proposals WHERE type = 'weekly_status_report' ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    return [r for r in rows if prefix is None or r[0].startswith(prefix)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--proposal")
    which.add_argument("--latest", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    found = _find(args.db, None if args.latest else args.proposal)
    if not found:
        print("no such weekly report")
        return 1
    if args.proposal and len(found) > 1:
        print(f"{args.proposal!r} matches {len(found)} reports; give more of the id")
        return 1
    proposal_id, status, week_ending = found[0]
    problems = verify_stored_report(proposal_id, db_path=args.db)
    print(f"weekly report {proposal_id[:8]} (week ending {week_ending}, {status}): " + ("every figure recomputes from its stored snapshots" if not problems else f"{len(problems)} problem(s)"))
    for problem in problems:
        print(f"  - {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
