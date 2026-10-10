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

When the scheduler starts it makes up the latest run each job missed while it was stopped (docs/catch_up.md): the first start only begins watching,
and a run more than six hours late waits for a person's approval. --no-catch-up turns that off.

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

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))


from pm.approval.settings import describe_settings
from pm.jobs.end_of_day_job import run_end_of_day_job
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling import catch_up as catch_up_on_startup
from pm.scheduling import weekly_report as weekly
from pm.scheduling.channel_briefs import add_channel_brief_jobs
from pm.scheduling.clock import (
    parse_at,
)
from pm.scheduling.config import (
    ProjectScheduleConfig,
    default_project_schedule_config,
)
from pm.scheduling.scheduler import build_scheduler
from pm.storage.db import DEFAULT_DB_PATH


class _ScriptedBoth:
    """The scripted stand-in for the brief and for the end-of-day summary, told apart by their prompts."""

    def __init__(self) -> None:
        from pm.eval.pm12_cases import ScriptedGateway
        from pm.reporting.scripted_summary import ScriptedSummaryGateway

        self.brief, self.summary = ScriptedGateway(), ScriptedSummaryGateway()

    def generate(self, prompt: str, **kwargs):
        return (self.summary if "end-of-day summary" in prompt else self.brief).generate(prompt, **kwargs)


def _gateway(kind: str):
    if kind == "scripted":
        return _ScriptedBoth()
    from spine.llm.gateway import LLMGateway

    return LLMGateway()


def _print_schedule(scheduler, config: ProjectScheduleConfig) -> None:
    now = datetime.now(timezone.utc)
    print(f"Project {config.channel_id} ({config.timezone}); working days: {', '.join(config.working_days)}")
    for job in scheduler.get_jobs():
        kind = "morning_brief" if "morning_brief" in job.id else "end_of_day"
        at = config.morning_brief_time if kind == "morning_brief" else config.end_of_day_time
        nxt = job.trigger.get_next_fire_time(None, now)
        print(f"  {kind:14} at {at.strftime('%H:%M')} {config.timezone}  next: {nxt.isoformat() if nxt else 'never'}")
    for line in describe_settings():
        print(f"  {line}")


def _weekly_gateway(kind: str):
    if kind == "none":
        return None
    if kind == "scripted":
        from pm.reporting.scripted_weekly import ScriptedWeeklyGateway

        return ScriptedWeeklyGateway()
    from spine.llm.gateway import LLMGateway

    return LLMGateway()


def _print_weekly(scheduler, schedule) -> None:
    job = scheduler.get_job(weekly.JOB_ID)
    nxt = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc)) if job else None
    print(f"  weekly_report    at {schedule.day} {schedule.at.strftime('%H:%M')} {schedule.timezone}  next: {nxt.isoformat() if nxt else 'never'}  (a draft proposal; never sent)")


def _print_channel_briefs(scheduler, entries) -> None:
    """The real-channel briefs: each channel's own timezone, and the next time each job fires."""
    now = datetime.now(timezone.utc)
    for channel_config, name in entries:
        print(f"Channel {name} ({channel_config.timezone}); working days: {', '.join(channel_config.working_days)}")
        for kind, at in (("morning", channel_config.morning_brief_time), ("evening", channel_config.end_of_day_time)):
            job = scheduler.get_job(f"pm:channel_{kind}:{channel_config.channel_id}")
            nxt = job.trigger.get_next_fire_time(None, now) if job else None
            print(f"  channel_{kind:8} at {at.strftime('%H:%M')} {channel_config.timezone}  next: {nxt.isoformat() if nxt else 'never'}")
    if entries:
        for line in describe_settings():
            print(f"  {line}")


def main(argv: list[str] | None = None, *, block: bool = True) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--gateway", choices=["llm", "scripted"], default="llm")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one job once and exit (the morning brief unless --job says otherwise)")
    mode.add_argument("--print-schedule", action="store_true", help="show the schedule and exit")
    parser.add_argument("--at", help="with --once: act as if it were this time, in the project's timezone")
    parser.add_argument("--job", choices=["morning", "end-of-day", "weekly"], default="morning", help="with --once: which job to run")
    parser.add_argument("--channel-briefs", default=os.environ.get("PM_CHANNEL_BRIEFS", ""), metavar="NAMES",
                        help="comma-separated real channels (P1 display names or ids) to make morning and evening briefs for, from P1's real "
                             "records (default: PM_CHANNEL_BRIEFS). Given, the seeded sample project is NOT scheduled unless --with-sample-project")
    parser.add_argument("--with-sample-project", action="store_true", help="with --channel-briefs: also schedule the seeded sample project's jobs")
    parser.add_argument("--weekly-report", action="store_true", default=weekly.enabled(),
                        help="also make the weekly status report once a week (PM_WEEKLY_REPORT_AT, default Fri 18:00 Asia/Colombo) and offer it as a proposal; "
                             "never sent (default: PM_WEEKLY_REPORT=1)")
    parser.add_argument("--weekly-gateway", choices=["llm", "scripted", "none"], default="llm",
                        help="who writes the weekly report's narrative: the model set by the environment, a stand-in, or nobody")
    parser.add_argument("--no-catch-up", action="store_true",
                        help="do not make up the jobs that were due while the scheduler was stopped (default: make up each job's latest missed run, up to "
                             "PM_CATCH_UP_HOURS back, default 72; one that is more than 6 hours late waits for a person's approval)")
    args = parser.parse_args(argv)
    channels = [c.strip() for c in args.channel_briefs.split(",") if c.strip()]

    config = default_project_schedule_config()

    if args.once:
        try:
            moment = parse_at(args.at, config.timezone) if args.at else datetime.now(timezone.utc)
        except ValueError:
            print(f"--at must look like 2026-09-16T08:00 (read in {config.timezone}); got {args.at!r}")
            return 2
        if args.job == "weekly":
            report = weekly.run_weekly_report_scheduled(db_path=args.db, timezone_name=weekly.weekly_schedule().timezone, gateway=_weekly_gateway(args.weekly_gateway),
                                                        moment=moment)
            print("weekly report: " + ("not made (see the log)" if report is None else
                  f"{'proposed' if report.created else 'already proposed'} {report.proposal_id}" if report.proposal_id else f"not offered: {len(report.problems)} problem(s)"))
            return 0 if report is not None else 1
        if args.job == "end-of-day":
            summary_result = run_end_of_day_job(config, _gateway(args.gateway), moment=moment, db_path=args.db)
            print(f"{summary_result.status}: {summary_result.detail}")
            if summary_result.summary is not None:
                print(f"morning snapshot: {summary_result.morning_source}")
                print(f"delivery: {summary_result.delivery_status}: {summary_result.delivery_detail}")
                if summary_result.proposal_id:
                    print(f"proposal: {summary_result.proposal_id}")
                    print("next:     uv run python scripts/approve.py list")
            return 0
        result = run_morning_brief_job(config, _gateway(args.gateway), moment=moment, db_path=args.db)
        print(f"{result.status}: {result.detail}")
        if result.brief is not None:
            print(f"delivery: {result.delivery_status}: {result.delivery_detail}")
            if result.proposal_id:
                print(f"proposal: {result.proposal_id}")
                print("next:     uv run python scripts/approve.py list")
        return 0

    sample = args.with_sample_project or not channels
    scheduler = build_scheduler([config] if sample else [], _gateway(args.gateway), db_path=args.db)
    entries = add_channel_brief_jobs(scheduler, channels, db_path=args.db) if channels else []
    weekly_schedule = weekly.add_weekly_report_job(scheduler, db_path=args.db, gateway=_weekly_gateway(args.weekly_gateway)) if args.weekly_report else None
    if args.print_schedule:
        if sample:
            _print_schedule(scheduler, config)
        _print_channel_briefs(scheduler, entries)
        if weekly_schedule:
            _print_weekly(scheduler, weekly_schedule)
        return 0

    catch_up = None if args.no_catch_up else catch_up_on_startup.enable(scheduler, args.db)  # records every run that finishes; queues the ones that were missed
    scheduler.start()
    print("Scheduler running. Ctrl-C to stop.")
    _print_catch_up(catch_up)
    if sample:
        _print_schedule(scheduler, config)
    _print_channel_briefs(scheduler, entries)
    if weekly_schedule:
        _print_weekly(scheduler, weekly_schedule)
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


def _print_catch_up(report) -> None:
    if report is None or not report.enabled:
        print("Catch-up on startup: off.")
        return
    if report.just_armed:
        print("Catch-up on startup: armed now. From here on, a job missed while this is stopped is made up at the next start.")
    for item in report.items:
        held = " (late: waits for a person's approval)" if item.late else ""
        print(f"Catch-up: making up {item.job_id}, scheduled for {item.scheduled_for.isoformat()}{held}")
    if not report.items and not report.just_armed:
        print("Catch-up on startup: nothing was missed.")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
