#!/usr/bin/env python3
"""Start P2's schedule, or run the morning brief once at a chosen moment.

  (no flags)          run the scheduler: the morning brief and the end-of-day
                      snapshot fire at their configured local times on working
                      days, until you press Ctrl-C
  --once [--at TIME]  run the morning job once now, or as if it were TIME (read
                      in the project's own timezone, e.g. 2026-09-16T08:00) --
                      the clock override. Leaves a pending proposal to approve
  --print-schedule    show what would run and when, and start nothing

The job only PROPOSES the brief. What happens next is the approval gate: you
approve it (scripts/approve.py), or, if PM_AUTO_APPROVE=1, the system does when
it is safe. Nothing reaches Teams unless TEAMS_PUBLISHER_MODE=power_automate;
the default is the log-only publisher. See .env.example.

--gateway scripted writes the brief from the facts instantly with no model (a
demo of the plumbing); --gateway llm (default) uses the model set by
LLM_PROVIDER, and on a laptop's local model takes about 15 minutes.

Usage:
    uv run python scripts/run_scheduler.py --once --at 2026-09-16T08:00 --gateway scripted
    uv run python scripts/run_scheduler.py --print-schedule
    uv run python scripts/run_scheduler.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from p1.adapters.teams_publisher_mock import LogPublisher

from pm.adapters.teams import get_teams_publisher
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import (
    ProjectScheduleConfig,
    default_project_schedule_config,
)
from pm.scheduling.scheduler import build_scheduler
from pm.storage.db import DEFAULT_DB_PATH


def parse_at(text: str, tz_name: str) -> datetime:
    """A wall-clock time like 2026-09-16T08:00, read in the project's timezone."""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(timezone.utc)


def _gateway(kind: str):
    if kind == "scripted":
        from pm.eval.pm12_cases import ScriptedGateway

        return ScriptedGateway()
    from spine.llm.gateway import LLMGateway

    return LLMGateway()


def _describe_settings() -> list[str]:
    """What is switched on, in words, without printing any secret."""
    try:
        publisher = get_teams_publisher()
        where = (
            "log-only (nothing reaches Teams)" if isinstance(publisher, LogPublisher)
            else "REAL: posts through the Power Automate flow"
        )
    except Exception as exc:  # noqa: BLE001 - misconfiguration is reported, not raised
        where = f"NOT AVAILABLE ({type(exc).__name__}: {exc})"
    auto = os.environ.get("PM_AUTO_APPROVE", "") == "1"
    first = os.environ.get("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", "1") != "0"
    approvers = [a.strip() for a in os.environ.get("PM_APPROVER_IDS", "").split(",") if a.strip()]
    return [
        f"publisher: {where}",
        f"auto-approve: {'ON' if auto else 'off'}"
        + (f" (a person must approve the first brief per channel: {'yes' if first else 'no'})" if auto else ""),
        f"approvers: {', '.join(approvers) if approvers else 'none set (PM_APPROVER_IDS)'}",
    ]


def _print_schedule(scheduler, config: ProjectScheduleConfig) -> None:
    now = datetime.now(timezone.utc)
    print(f"Project {config.channel_id} ({config.timezone}); working days: {', '.join(config.working_days)}")
    for job in scheduler.get_jobs():
        kind = "morning_brief" if "morning_brief" in job.id else "end_of_day"
        at = config.morning_brief_time if kind == "morning_brief" else config.end_of_day_time
        nxt = job.trigger.get_next_fire_time(None, now)
        print(f"  {kind:14} at {at.strftime('%H:%M')} {config.timezone}  next: {nxt.isoformat() if nxt else 'never'}")
    for line in _describe_settings():
        print(f"  {line}")


def main(argv: list[str] | None = None, *, block: bool = True) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--gateway", choices=["llm", "scripted"], default="llm")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run the morning job once and exit")
    mode.add_argument("--print-schedule", action="store_true", help="show the schedule and exit")
    parser.add_argument("--at", help="with --once: act as if it were this time, in the project's timezone")
    args = parser.parse_args(argv)

    config = default_project_schedule_config()

    if args.once:
        try:
            moment = parse_at(args.at, config.timezone) if args.at else datetime.now(timezone.utc)
        except ValueError:
            print(f"--at must look like 2026-09-16T08:00 (read in {config.timezone}); got {args.at!r}")
            return 2
        result = run_morning_brief_job(config, _gateway(args.gateway), moment=moment, db_path=args.db)
        print(f"{result.status}: {result.detail}")
        if result.brief is not None:
            print(f"delivery: {result.delivery_status}: {result.delivery_detail}")
            if result.proposal_id:
                print(f"proposal: {result.proposal_id}")
                print("next:     uv run python scripts/approve.py list")
        return 0

    scheduler = build_scheduler([config], _gateway(args.gateway), db_path=args.db)
    if args.print_schedule:
        _print_schedule(scheduler, config)
        return 0

    scheduler.start()
    print("Scheduler running. Ctrl-C to stop.")
    _print_schedule(scheduler, config)
    if not block:
        scheduler.shutdown(wait=False)
        return 0
    try:
        while True:
            _time.sleep(3600)
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        scheduler.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
