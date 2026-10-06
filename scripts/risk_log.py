#!/usr/bin/env python3
"""The risk log: show it, compare its three copies, and keep them in sync.

  repo CSV       risk_log/risks.csv: committed, the system of record, opens in Excel
  lead store     an editable table (Supabase's p2.risk_log) the delivery lead opens and edits;
                 it stands in for the Dataverse table the plan names
  runtime copy   the risks table in data/pm.db that the morning brief reads

  show               the log as a table
  status             compare the three; exit 1 if they differ
  sync [--dry-run]   three-way sync: a lead's edit flows into the repo, a repo edit flows to
                     the lead, and if BOTH changed it reports a conflict and changes nothing
  push [--dry-run]   force the repo log onto the lead's table
  pull [--dry-run]   force the lead's table onto the repo log (and the runtime copy)

Bad edits (an unknown severity, a risk pointing at a tracker item that does not exist)
are refused with every problem named. Needs SUPABASE_DB_URL for the lead store; the URL
is a credential and is never printed.

Usage:
    uv run python scripts/risk_log.py show
    uv run python scripts/risk_log.py status
    uv run python scripts/risk_log.py sync --dry-run
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

from pm.mirror.supabase_mirror import _scrub
from pm.risklog.csv_store import DEFAULT_CSV_PATH, CsvRiskLog, RiskLogDataError
from pm.risklog.supabase_store import SupabaseRiskLog
from pm.risklog.sync import (
    CONFLICT,
    DEFAULT_BASELINE_PATH,
    IN_SYNC,
    PULLED,
    PUSHED,
    RiskLogSync,
)
from pm.storage.db import DEFAULT_DB_PATH

GOOD = {IN_SYNC, PUSHED, PULLED}


def _safe(text: str) -> str:
    return _scrub(text, os.environ.get("SUPABASE_DB_URL", "") or "\0")


def _print_show(csv: CsvRiskLog) -> None:
    risks = csv.list_risks()
    print(f"{'id':9} {'severity':8} {'status':9} {'item':7} {'opened':10}  title")
    for r in risks:
        print(f"{r.id:9} {r.severity:8} {r.status:9} {(r.related_item_id or '-'):7} {r.opened_at:10}  {r.title}")
    print(f"\n{len(risks)} risk(s). Source: {DEFAULT_CSV_PATH if csv._path == DEFAULT_CSV_PATH else csv._path}")


def main(argv: list[str] | None = None, *, remote=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--csv", default=str(DEFAULT_CSV_PATH))
    parser.add_argument("--baseline", default=os.environ.get("PM_RISK_LOG_BASELINE") or str(DEFAULT_BASELINE_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("status")
    for name in ("sync", "push", "pull"):
        sub.add_parser(name).add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    csv = CsvRiskLog(args.csv)
    if args.command == "show":
        try:
            _print_show(csv)
        except RiskLogDataError as exc:
            print(f"The repo risk log is invalid: {exc}")
            return 1
        return 0

    if remote is None:
        url = os.environ.get("SUPABASE_DB_URL", "")
        if not url:
            print("SUPABASE_DB_URL is not set (see .env.example), so there is no lead-facing store to compare or sync.")
            return 1
        remote = SupabaseRiskLog(url, schema=os.environ.get("PM_SUPABASE_SCHEMA", "p2"))
    sync = RiskLogSync(csv, remote, db_path=args.db, baseline_path=args.baseline)

    if args.command == "status":
        comparison = sync.compare()
        for store, count in comparison.counts.items():
            print(f"  {store:13} {'unreachable' if count is None else str(count) + ' risk(s)'}")
        if comparison.in_sync:
            print("All three copies are in sync.")
            return 0
        print("Not in sync:" if comparison.remote_reachable else "The lead store could not be reached.")
        for line in comparison.differences:
            print(f"  - {_safe(line)}")
        return 1

    outcome = getattr(sync, args.command)(dry_run=args.dry_run)
    prefix = "DRY RUN (nothing written): " if outcome.dry_run else ""
    print(f"{prefix}{outcome.action}: {_safe(outcome.detail)}")
    for line in (*outcome.problems, *outcome.changes):
        print(f"  - {_safe(line)}")
    return 0 if outcome.action in GOOD and outcome.action != CONFLICT else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
