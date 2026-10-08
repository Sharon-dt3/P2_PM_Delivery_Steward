#!/usr/bin/env python3
"""PM-29: the weekly status report, from three stored snapshots a week apart. Never sent: it is shown, and offered as a proposal to read or reject.

  --at MOMENT    act as if it were this moment, in the project's timezone (default: now)
  --db PATH      the database to read and store snapshots in
  --dry-run      show the report and check it; store nothing and propose nothing (snapshots are built in memory only)

Usage:
    uv run python scripts/weekly_report.py --db /tmp/scratch.db --at 2026-09-18T17:00
    uv run python scripts/weekly_report.py --dry-run --at 2026-09-18T17:00
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.jobs.weekly_report_job import WEEK, run_weekly_report_job
from pm.reporting.weekly import compute_weekly_facts, render_weekly_report
from pm.reporting.weekly_check import check_report
from pm.state.snapshot import build_current_snapshot
from pm.storage.db import DEFAULT_DB_PATH

TZ = "Asia/Colombo"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--at")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--tz", default=TZ)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    moment = datetime.now(timezone.utc)
    if args.at:
        parsed = datetime.fromisoformat(args.at)
        moment = (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(args.tz))).astimezone(timezone.utc)

    if args.dry_run:
        snaps = [build_current_snapshot(args.db, taken_at=(moment - n * WEEK).isoformat(), tz_name=args.tz) for n in (0, 1, 2)]
        facts = compute_weekly_facts(*snaps)
        text, problems, note = render_weekly_report(facts), check_report(facts, render_weekly_report(facts), *snaps), "dry run: nothing stored, nothing proposed"
    else:
        report = run_weekly_report_job(moment, db_path=args.db, timezone_name=args.tz)
        text, problems = report.text, report.problems
        note = f"proposed {report.proposal_id}" if report.created else (f"already proposed {report.proposal_id}" if report.proposal_id else "not proposed")
    print(text)
    print()
    print(f"recomputed from the snapshots: {'every figure matches' if not problems else str(len(problems)) + ' problem(s)'}; {note}")
    for problem in problems:
        print(f"  - {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
