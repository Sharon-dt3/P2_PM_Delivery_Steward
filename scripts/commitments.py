#!/usr/bin/env python3
"""Commitments: the ageing view, feeding from channel outcome records, follow-ups, and closing one by hand.

  ageing [--at TIME]                 every open commitment: how long open, how overdue, what was done about it
  ingest PATH                        add the commitments in an outcome record file (or every .json in a folder)
  ingest-recent [--days N]           read P1's last N days of outcome records for this project
  run [--at TIME] [--dry-run]        the follow-up pass: reminders before the due date, overdue records,
                                     escalation to the lead beyond the threshold. --dry-run changes and sends nothing
  close ID [--cancel]                mark a commitment fulfilled (or cancelled) by hand

TIME is read in the project's timezone, e.g. 2026-09-18T09:00. Reminders are direct messages through P1's publish
adapter (TEAMS_PUBLISHER_MODE decides whether that is the log-only publisher or Teams); the first reminder to a
person, and the first escalation about a person, wait for a person to approve them (scripts/approve.py list).
The shared per-person per-day cap counts this agent's reminders and P1's (P1's ledger: P1_DB_PATH).

Usage:
    uv run python scripts/commitments.py ageing
    uv run python scripts/commitments.py run --dry-run
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

from pm.adapters.tracker import ItemNotFoundError, TrackerMock
from pm.commitments.ageing import ageing_view, format_ageing
from pm.commitments.followup import load_followup_settings
from pm.commitments.job import outcomes_dir, run_commitment_pass
from pm.commitments.outcomes import (
    ingest_recent_outcomes,
    ingest_record_file,
    message_lookup,
)
from pm.commitments.store import (
    CANCELLED,
    FULFILLED,
    CommitmentNotFoundError,
    CommitmentTracker,
)
from pm.seed.build import CHANNEL_ID
from pm.storage.db import DEFAULT_DB_PATH


def _moment(text: str | None, tz_name: str) -> datetime:
    if not text:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(timezone.utc)


def _lookups(db_path):
    items = TrackerMock(db_path=db_path)
    names = {a.id: a.display_name for a in items.list_assignees()}

    def item_status(item_id: str) -> str | None:
        try:
            return items.get_item(item_id).status
        except ItemNotFoundError:
            return None

    return names, item_status


def _print_ingest(results) -> None:
    for r in results:
        print(f"  {r.message_id or '(record)':<18} {r.status:<18} {r.detail}" + (f"  -> commitment #{r.commitment_id}" if r.commitment_id else ""))
    added = sum(1 for r in results if r.status == "added")
    print(f"{added} commitment(s) added, {len(results)} line(s) looked at.")


def main(argv: list[str] | None = None, *, publisher=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    ageing = sub.add_parser("ageing")
    ageing.add_argument("--at")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("path")
    recent = sub.add_parser("ingest-recent")
    recent.add_argument("--days", type=int, default=7)
    recent.add_argument("--at")
    run = sub.add_parser("run")
    run.add_argument("--at")
    run.add_argument("--dry-run", action="store_true")
    close = sub.add_parser("close")
    close.add_argument("id", type=int)
    close.add_argument("--cancel", action="store_true")
    args = parser.parse_args(argv)

    settings = load_followup_settings(CHANNEL_ID)
    tracker = CommitmentTracker(args.db)
    names, item_status = _lookups(args.db)

    if args.command == "ageing":
        today = _moment(args.at, settings.timezone).astimezone(ZoneInfo(settings.timezone)).date()
        print(format_ageing(ageing_view(tracker=tracker, today=today, item_status=item_status, names=names), today))
        return 0

    if args.command in ("ingest", "ingest-recent"):
        lookup = message_lookup(CHANNEL_ID, args.db)
        if args.command == "ingest":
            path = Path(args.path)
            files = sorted(path.glob("*.json")) if path.is_dir() else [path]
            results = [r for f in files for r in ingest_record_file(
                f, tracker=tracker, message_info=lookup, roster=set(names), item_exists=lambda i: item_status(i) is not None)]
        else:
            today = _moment(args.at, settings.timezone).astimezone(ZoneInfo(settings.timezone)).date()
            results = ingest_recent_outcomes(
                channel_id=CHANNEL_ID, today=today, days_back=args.days, output_dir=outcomes_dir(), tracker=tracker, message_info=lookup,
                roster=set(names), item_exists=lambda i: item_status(i) is not None)
        _print_ingest(results)
        return 0

    if args.command == "close":
        try:
            tracker.get(args.id)
        except CommitmentNotFoundError:
            print(f"No commitment #{args.id}")
            return 1
        today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
        closed = tracker.close(args.id, status=CANCELLED if args.cancel else FULFILLED, day=today, detail="closed by hand")
        print(f"#{args.id} " + ("closed." if closed else "was not open: nothing changed."))
        return 0 if closed else 1

    moment = _moment(args.at, settings.timezone)
    results = run_commitment_pass(moment, db_path=args.db, publisher=publisher, dry_run=args.dry_run, ingest=False)
    print(f"Commitment follow-up as of {moment.astimezone(ZoneInfo(settings.timezone)).isoformat(timespec='minutes')}"
          f" (shared cap {settings.cap_per_person_per_day} a person a day; escalate beyond {settings.escalation_threshold_days} days overdue"
          f" to {names.get(settings.lead_id, settings.lead_id)})" + (" - DRY RUN, nothing changed or sent" if args.dry_run else ""))
    for r in results:
        print(f"  #{r.commitment_id:<3} {names.get(r.member_id, r.member_id):<16} {r.action:<10} {r.status:<26} {r.detail}")
    if not results:
        print("  no open commitments.")
    if any(r.status == "awaiting_approval" for r in results):
        print("Waiting for a person: uv run python scripts/approve.py list")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
    raise SystemExit(main())
