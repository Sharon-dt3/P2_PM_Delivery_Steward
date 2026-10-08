#!/usr/bin/env python3
"""Make a channel brief for a real Teams channel, from P1's real record of what the team said. Nothing is sent: it is PROPOSED.

  --channel NAME   a channel on P1's allowlist, by display name or id (e.g. p1-agent-test)
  --kind           morning (what the team said on the last working day) or evening (today, after P1's digest)
  --at TIME        act as if it were this moment, in the channel's timezone (e.g. 2026-10-08T08:00)
  --dry-run        show the message and propose nothing

With nothing real to report from (no record, a stale one, one P1 was not cleared to pass on) it proposes nothing and says why.

Usage:
    uv run python scripts/run_channel_brief.py --channel p1-agent-test --kind morning --dry-run
    uv run python scripts/run_channel_brief.py --channel p1-agent-test --kind morning --db data/pm.db
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.channelbrief.facts import EVENING, MORNING
from pm.jobs.channel_brief_job import run_channel_brief_job
from pm.scheduling.channel_briefs import (
    UnknownChannel,
    channel_schedule_config,
    resolve_channel,
)
from pm.scheduling.clock import parse_at
from pm.storage.db import DEFAULT_DB_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", required=True)
    parser.add_argument("--kind", choices=[MORNING, EVENING], default=MORNING)
    parser.add_argument("--at")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        channel_id, name = resolve_channel(args.channel)
    except UnknownChannel as exc:
        print(exc)
        return 2
    config = channel_schedule_config(channel_id)
    try:
        moment = parse_at(args.at, config.timezone) if args.at else datetime.now(timezone.utc)
    except ValueError:
        print(f"--at must look like 2026-10-08T08:00 (read in {config.timezone}); got {args.at!r}")
        return 2
    result = run_channel_brief_job(config, name, args.kind, moment=moment, db_path=args.db, dry_run=args.dry_run)
    print(f"{result.status}: {result.detail}")
    if result.message:
        print("\n" + result.message + "\n")
    if result.proposal_id:
        print(f"delivery: {result.delivery_status}: {result.delivery_detail}")
        print(f"proposal: {result.proposal_id}")
    return 0 if result.status in ("proposed_from_record", "no_data", "skipped_non_working_day") else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
