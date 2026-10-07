#!/usr/bin/env python3
"""Consume a channel outcome record from P1 (PM-26): show what it would propose, and propose it.

P1 writes one JSON file per channel per day (outcomes/<channel>/<date>.json, published schema in P1's schema/). This reads one,
validates it against that schema (pm.channel.record: no P1 code), and plans two batched proposal sets:

  tracker changes   a comment on each tracker item a line names; a new item for a blocker that names none
  risk-log entries  a risk entry for each blocker on an item with no open entry (or naming none)

Every item carries the channel and the message that justify it. Nothing is written to the tracker or the risk log: the two
proposals wait for a person, like every other. A record P1 was not cleared to pass on, or that is not the published schema,
is refused: zero proposals and one row in the audit log.

  --dry-run   show the plan and the proposals it would make; create nothing

Usage:
    uv run python scripts/consume_outcome.py ../P3_Agents/outcomes/<channel>/2026-10-07.json --dry-run
    uv run python scripts/consume_outcome.py --channel-id 19:...@thread.tacv2 --date 2026-10-07
    uv run python scripts/consume_outcome.py --latest
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

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.tracker import TrackerMock
from pm.channel.batches import TrackerView, consume
from pm.channel.record import record_file
from pm.storage.db import DEFAULT_DB_PATH


def outcomes_dir() -> Path:
    """Where P1 writes its records: P1_OUTCOMES_DIR, else the outcomes folder in P1's repo next to this one."""
    return Path(os.environ.get("P1_OUTCOMES_DIR") or _P1_REPO_ROOT / "outcomes")


def record_path(channel_id: str, day: str) -> Path:
    """The published layout: outcomes/<channel id with every character outside [A-Za-z0-9_.-] replaced by _>/<date>.json."""
    return record_file(outcomes_dir(), channel_id, day)


def _show_items(title: str, items: list[dict]) -> None:
    print(f"\n{title} ({len(items)})")
    for item in items:
        ref = item["reference"]
        what = {"comment": f"comment on {item.get('item_id')}", "create": "new item (blocked, nobody assigned)",
                "new_risk": f"new risk entry for {item.get('related_item_id') or 'no item'}"}[item["kind"]]
        text = item.get("body") or item.get("title") or item["source_text"]
        print(f"  - {what}: {text[:110]}")
        print(f"      from {ref['channel_display_name']} message {ref['message_id']} on {ref['date']} ({ref['section']})"
              + (f", quote: \"{ref['quote'][:60]}\"" if ref.get("quote") else "") + f"  [{ref['ref'][:60]}]")
        if item.get("changed_since"):
            print(f"      replaces an earlier wording ({item['changed_since']['earlier_status']}): \"{item['changed_since']['earlier_text'][:70]}\"")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", help="a record file")
    parser.add_argument("--channel-id")
    parser.add_argument("--date")
    parser.add_argument("--latest", action="store_true", help="the newest record in P1's outcomes folder")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.path:
        path = Path(args.path)
    elif args.channel_id and args.date:
        path = record_path(args.channel_id, args.date)
    elif args.latest:
        found = sorted(outcomes_dir().glob("*/*.json"), key=lambda p: (p.stat().st_mtime, str(p)))
        if not found:
            print(f"no records under {outcomes_dir()}")
            return 2
        path = found[-1]
    else:
        parser.error("give a record file, or --channel-id and --date, or --latest")

    view = TrackerView.from_adapters(TrackerMock(db_path=args.db), RiskLogMock(db_path=args.db))
    result = consume(path, view, db_path=args.db, dry_run=args.dry_run)

    print(f"record: {path}")
    if result.refused:
        print(f"REFUSED ({result.refused.code}): {result.refused.reason}")
        print("zero proposals" + ("" if args.dry_run else "; one row written to the audit log"))
        return 2
    record = result.record
    print(f"{record.channel_display_name} {record.date}  schema {record.schema_version}  "
          f"{len(record.updates)} updates, {len(record.blockers)} blockers, {len(record.decisions)} decisions, {len(record.questions)} questions")
    _show_items("tracker changes", result.plan.tracker_items)
    _show_items("risk-log entries", result.plan.risk_items)
    if result.skipped:
        print(f"\nnot proposed ({len(result.skipped)})")
        counts: dict[tuple[str, str], list[str]] = {}
        for s in result.skipped:
            counts.setdefault((s["set"], s["reason"]), []).append(s["section"])
        for (set_name, reason), sections in counts.items():
            kinds = ", ".join(f"{sections.count(k)} {k}" for k in dict.fromkeys(sections))
            print(f"  - {set_name}: {len(sections)} message(s) ({kinds}): {reason}")
    print()
    for batch in (result.tracker, result.risk):
        if args.dry_run:
            print(f"{batch.set_name} batch: would propose {batch.items} item(s)")
        elif batch.proposal_id and batch.created:
            print(f"{batch.set_name} batch: proposed {batch.items} item(s) as proposal {batch.proposal_id[:8]} (pending a person's decision)"
                  + (f"; {batch.remembered} already proposed before" if batch.remembered else ""))
        elif batch.proposal_id:
            print(f"{batch.set_name} batch: already proposed as {batch.proposal_id[:8]}; nothing new")
        else:
            print(f"{batch.set_name} batch: nothing to propose" + (f" ({batch.remembered} already proposed or decided against)" if batch.remembered else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
