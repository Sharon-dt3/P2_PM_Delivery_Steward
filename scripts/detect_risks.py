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

Promotion (PM-19): a blocker is only proposed once it has been blocked for MORE days than the
configured threshold (config/risk_promotion.yaml, or the environment variable
PM_RISK_PROMOTION_THRESHOLD_DAYS). Age is counted from the item's status transitions.
  --promotion-config P  read the threshold from this file instead
  --threshold-days N    use N for this run only
  --no-threshold        no age requirement for this run: every blocker missing from the log (PM-16)

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
from pm.risk.promotion import plan_promotion
from pm.risk.promotion_config import (
    PromotionConfigError,
    PromotionPolicy,
    load_promotion_policy,
)
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


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _policy(args) -> PromotionPolicy | None:
    if args.no_threshold:
        return None
    if args.threshold_days is not None:
        return PromotionPolicy(args.threshold_days, "command line --threshold-days")
    return load_promotion_policy(path=args.promotion_config)


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _print_plan(plan, db_path, *, show_memory: bool) -> None:
    threshold = plan.policy.threshold_days
    print(f"Promotion threshold: {_days(threshold)} ({plan.policy.source}); a blocker is promoted once it is OLDER than that")
    for c in plan.eligible:
        print(f"  {c.gap.item_id}  {_days(c.age.days)} old, blocked since {c.age.entered_on}: promote (older than {_days(threshold)})")
        if show_memory:
            print(f"      {_remembered(recall_for(c.gap, db_path=db_path))}")
    for c in plan.below_threshold:
        print(f"  {c.gap.item_id}  {_days(c.age.days)} old, blocked since {c.age.entered_on}: not yet "
              f"(needs to be older than {_days(threshold)})")
    for gap, age in plan.age_unknown:
        print(f"  {gap.item_id}  age unknown: not promoted ({age.note})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Propose risk-log entries for unlogged blockers.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--at", help="moment to evaluate (default: now)")
    parser.add_argument("--tz", default="Asia/Colombo")
    parser.add_argument("--gateway", choices=["llm", "scripted"], default="llm")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--promotion-config", help="file to read the promotion threshold from")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--threshold-days", type=_non_negative, help="promotion threshold for this run, in days")
    group.add_argument("--no-threshold", action="store_true", help="no age requirement for this run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    moment = datetime.fromisoformat(args.at) if args.at else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    snapshot = build_current_snapshot(args.db, taken_at=moment.isoformat(), tz_name=args.tz)
    try:
        policy = _policy(args)
    except PromotionConfigError as exc:
        print(f"Promotion configuration problem, nothing was proposed: {exc}")
        return 2

    blockers = current_blockers(snapshot)
    gaps = find_gaps(snapshot)
    print(f"Current blockers: {len(blockers)}; already in the risk log: {len(blockers) - len(gaps)}")
    print(f"Missing from the risk log: {', '.join(g.item_id for g in gaps) or 'none'}")

    if policy is None:
        print("Promotion threshold: none (every blocker missing from the risk log is proposed)")
        if args.dry_run:
            for gap in gaps:
                owner = gap.owner.name if gap.owner else "no owner evidenced"
                print(f"  {gap.item_id}  {gap.title}  (owner: {owner}; {gap.reference})")
                print(f"      {_remembered(recall_for(gap, db_path=args.db))}")
    else:
        plan = plan_promotion(snapshot, policy, db_path=args.db)
        _print_plan(plan, args.db, show_memory=args.dry_run)
        gaps = [c.gap for c in plan.eligible]
    if args.dry_run:
        print("Dry run: nothing was proposed.")
        return 0

    results = detect_and_propose(snapshot, _gateway(args.gateway, gaps), db_path=args.db, promotion=policy)
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
