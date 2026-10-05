#!/usr/bin/env python3
"""Browse data/pm.db in your browser: every table, with search and sorting.

Read-only, and served only on this machine (127.0.0.1), so nothing leaves the Mac.
It always shows the live file, so you can watch rows appear as jobs and approvals
run -- reload the page. Uses the sqlite-web package, fetched on demand by uv, so
nothing is added to the project's dependencies.

Usage:
    uv run --with sqlite-web python scripts/view_db.py
    then open http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = _REPO_ROOT / "data" / "pm.db"
DEFAULT_PORT = 8081


_LAUNCH = "import sys; from sqlite_web.sqlite_web import main; main()"


def build_command(db_path: str | Path, *, port: int = DEFAULT_PORT) -> list[str]:
    """sqlite-web's own `python -m sqlite_web` entry point is broken in 0.8.2, so
    its real main() is called directly."""
    return [
        sys.executable, "-c", _LAUNCH,
        "--read-only", "--host", "127.0.0.1", "--port", str(port), "--no-browser", str(db_path),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dry-run", action="store_true", help="show what would be served and stop")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"Database not found at {args.db}")
        return 1
    print(f"Serving {args.db} read-only at http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    if args.dry_run:
        return 0
    try:
        return subprocess.call(build_command(args.db, port=args.port))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
