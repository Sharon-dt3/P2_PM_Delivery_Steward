#!/usr/bin/env python3
"""PM-16: propose a risk-log entry for every current blocker that has none.

Python finds the gap (blocked items with no risk-log entry) and does all the arithmetic;
the model only phrases the description and impact, and is held to the evidence. Each
proposal carries the blocker reference, a suggested owner only where the tracker names
one, and the evidence a person can check. Nothing is written to the risk log: proposals
wait for a person (scripts/approve.py list, or the dashboard) and can be rejected.

  --dry-run             list the gaps, create nothing, call no model
  --gateway scripted    write the prose from the facts, instantly, no model (plumbing demo)
  --gateway llm         the model set by the environment (default; local Ollama today)
  --at MOMENT           the moment to evaluate, e.g. 2026-09-18T12:00 (UTC when no offset)
  --tz ZONE             the project's timezone (default Asia/Colombo)

Usage:
    uv run python scripts/detect_risks.py --dry-run
    uv run python scripts/detect_risks.py --gateway scripted
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

from spine.approval.proposals import ProposalStore

from pm.risk import memory
from pm.risk.gaps import current_blockers, find_gaps
from pm.risk.proposals import detect_and_propose, recall_for
from pm.state.snapshot import build_current_snapshot
from pm.storage.db import DEFAULT_DB_PATH


def _gateway(kind: str, gaps):
    if kind == "scripted":
        from pm.risk.scripted import ScriptedRiskGateway

        return ScriptedRiskGateway(gaps)
    from spine.llm.gateway import LLMGateway

    return LLMGateway()


def _remembered(recalled) -> str:
    """What the rejection memory says about a blocker, in words."""
    prior = recalled.proposal.id[:8] if recalled.proposal else ""
    return {
        memory.NEW: "new: would be proposed",
        memory.ALREADY_PROPOSED: f"already proposed (proposal {prior}), still open",
        memory.REJECTED_UNCHANGED: f"rejected earlier (proposal {prior}), nothing material changed: not proposed again",
        memory.AWAITING_DECISION: f"an earlier proposal ({prior}) is still awaiting a decision: not proposed again",
        memory.CHANGED_SINCE_REJECTION: f"rejected earlier (proposal {prior}) but changed since: would be proposed again",
    }[recalled.state]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Propose risk-log entries for unlogged blockers.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--at", help="moment to evaluate (default: now)")
    parser.add_argument("--tz", default="Asia/Colombo")
    parser.add_argument("--gateway", choices=["llm", "scripted"], default="llm")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    moment = datetime.fromisoformat(args.at) if args.at else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    snapshot = build_current_snapshot(args.db, taken_at=moment.isoformat(), tz_name=args.tz)

    blockers = current_blockers(snapshot)
    gaps = find_gaps(snapshot)
    print(f"Current blockers: {len(blockers)}; already in the risk log: {len(blockers) - len(gaps)}")
    print(f"Missing from the risk log: {', '.join(g.item_id for g in gaps) or 'none'}")

    if args.dry_run:
        for gap in gaps:
            owner = gap.owner.name if gap.owner else "no owner evidenced"
            print(f"  {gap.item_id}  {gap.title}  (owner: {owner}; {gap.reference})")
            print(f"      {_remembered(recall_for(gap, db_path=args.db))}")
        print("Dry run: nothing was proposed.")
        return 0

    results = detect_and_propose(snapshot, _gateway(args.gateway, gaps), db_path=args.db)
    store = ProposalStore(args.db)
    for r in results:
        if r.created:
            again = r.state == memory.CHANGED_SINCE_REJECTION
            prose = "/".join(f"{k}:{v}" for k, v in r.prose_source.items())
            print(f"  {r.item_id}  {'proposed again, changed since it was rejected' if again else 'proposed'}"
                  f"  (proposal {r.proposal_id[:8]}, prose {prose})")
            if again:
                print(f"      {store.get(r.proposal_id).payload['change']['text']}")
        else:
            print(f"  {r.item_id}  {_remembered(memory.Memory(r.state, store.get(r.proposal_id)))}")
    if any(r.created for r in results):
        print("Waiting for a person: uv run python scripts/approve.py list")
    elif results:
        print("Nothing new proposed.")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
    raise SystemExit(main())
