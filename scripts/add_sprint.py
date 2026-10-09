#!/usr/bin/env python3
"""Add a sprint to the tracker, and optionally move items into it. Nothing here picks a sprint for you: you give the name and the dates.

  --id ID         the sprint's id, like sprint-14
  --name NAME     what it is called, like "Sprint 14"
  --start DATE    first day (inclusive), like 2026-10-05
  --end DATE      last day (inclusive), like 2026-10-18
  --move IDS      also move these items (comma-separated, like PM-031,PM-032) into it
  --by WHO        recorded in the audit log as who did it (default: operator)
  --db PATH       the database (default data/pm.db)
  --dry-run       check everything and change nothing

A new sprint's range may not overlap another sprint's. Examples:
    uv run python scripts/add_sprint.py --id sprint-14 --name "Sprint 14" --start 2026-10-05 --end 2026-10-18 --move PM-031,PM-032 --by sharon
    uv run python scripts/add_sprint.py --id sprint-15 --name "Sprint 15" --start 2026-10-19 --end 2026-11-01 --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.storage.db import DEFAULT_DB_PATH
from pm.tracker_admin import TrackerAdminRefused, add_sprint, move_items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--id", required=True, dest="sprint_id")
    parser.add_argument("--name", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--move", default="")
    parser.add_argument("--by", default="operator")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    items = [i.strip() for i in args.move.split(",") if i.strip()]

    db = args.db
    scratch = None
    if args.dry_run:  # the real checks, run on a copy, so what is said is what would happen
        scratch = tempfile.TemporaryDirectory(prefix="pm_add_sprint_")
        db = str(Path(scratch.name) / "copy.db")
        shutil.copyfile(args.db, db)
    try:
        add_sprint(db, sprint_id=args.sprint_id, name=args.name, start=args.start, end=args.end, by=args.by)
        moved = move_items(db, items, sprint_id=args.sprint_id, by=args.by) if items else None
    except TrackerAdminRefused as exc:
        print(f"not done: {exc}")
        return 1
    finally:
        if scratch:
            scratch.cleanup()
    prefix = "dry run, nothing changed: would add" if args.dry_run else "added"
    print(f"{prefix} {args.name} ({args.sprint_id}), {args.start} to {args.end}")
    if moved:
        print(f"  {'would move' if args.dry_run else 'moved'}: {', '.join(moved.moved) or 'none'}"
              + (f"; already there: {', '.join(moved.already_there)}" if moved.already_there else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
