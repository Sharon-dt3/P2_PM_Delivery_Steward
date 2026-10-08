#!/usr/bin/env python3
"""Run the Copilot Studio / Power Automate API (PM-28) with this repo's .env loaded.

`uvicorn pm.api.copilot_studio_api:app` alone does not read .env; this does. Settings (read from .env, never printed):
PM_COPILOT_API_KEY (required: the API refuses every action without it), PM_DB_PATH (default data/pm.db), PM_APPROVER_IDS,
TEAMS_PUBLISHER_MODE (mock = log-only, nothing reaches Teams; power_automate = P1's flow).

Usage:
    uv run python scripts/run_copilot_api.py [--port 8765] [--db data/pm_demo.db]
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

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", help="overrides PM_DB_PATH")
    args = parser.parse_args(argv)
    if args.db:
        os.environ["PM_DB_PATH"] = str(Path(args.db).resolve())
    if not os.environ.get("PM_COPILOT_API_KEY"):
        print("PM_COPILOT_API_KEY is not set in .env: the API would refuse every action. Set it first.")
        return 2
    from pm.api.copilot_studio_api import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
