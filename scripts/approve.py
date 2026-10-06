#!/usr/bin/env python3
"""Approve, reject, or edit-then-approve what the agent proposes to send.

The command-line surface of the approval gate: it calls exactly the service
the Teams card calls, so a decision made here leaves the same records.

  list                              proposals awaiting a decision
  show   ID                         one proposal's text
  approve ID --as WHO [--edit TEXT | --edit-file PATH]
  reject  ID --as WHO [--reason TEXT]
  retry   ID                        send an approved brief whose send failed
  audit   ID                        who decided, when, original vs applied

WHO must be listed in PM_APPROVER_IDS (comma-separated). The publisher is
chosen by TEAMS_PUBLISHER_MODE as everywhere else: log-only unless set to
power_automate (see .env.example).

Usage:
    uv run python scripts/approve.py list
    uv run python scripts/approve.py approve PROPOSAL_ID --as sharon.silva
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from spine.approval.proposals import ProposalNotFoundError, ProposalStore

from pm.approval import service
from pm.approval.audit import audit_trail, describe


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="database path (default: data/pm.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("proposal_id")
    approve = sub.add_parser("approve")
    approve.add_argument("proposal_id")
    approve.add_argument("--as", dest="approver", required=True)
    group = approve.add_mutually_exclusive_group()
    group.add_argument("--edit", help="replacement text to send instead of the proposal")
    group.add_argument("--edit-file", help="file holding the replacement text")
    reject = sub.add_parser("reject")
    reject.add_argument("proposal_id")
    reject.add_argument("--as", dest="approver", required=True)
    reject.add_argument("--reason")
    retry = sub.add_parser("retry")
    retry.add_argument("proposal_id")
    audit = sub.add_parser("audit")
    audit.add_argument("proposal_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kwargs = {"db_path": args.db} if args.db else {}

    if args.command == "list":
        pending = service.list_pending_approvals(**kwargs)
        for p in pending:
            what = f"to {p.target_channel}" if p.type in service.EXECUTABLE_TYPES else p.summary
            print(f"{p.proposal_id}  {p.local_date}  {what}  proposed {p.created_at}")
        if not pending:
            print("Nothing is awaiting a decision.")
        return 0

    if args.command in ("show", "audit"):
        try:
            if args.command == "show":
                print(ProposalStore(args.db or "data/pm.db").get(args.proposal_id).payload.get("content", ""))
            else:
                print(describe(audit_trail(args.proposal_id, **kwargs)))
        except ProposalNotFoundError:
            print(f"No proposal with id {args.proposal_id!r}")
            return 1
        return 0

    if args.command == "retry":
        result = service.send_approved(args.proposal_id, **kwargs)
    elif args.command == "approve":
        edited = args.edit
        if args.edit_file:
            edited = Path(args.edit_file).read_text()
        result = service.approve_and_send(args.proposal_id, approver_id=args.approver, edited_content=edited, **kwargs)
    else:
        result = service.reject(args.proposal_id, approver_id=args.approver, reason=args.reason, **kwargs)

    print(f"{result.outcome}: {result.detail}")
    return 0 if result.outcome in (service.SENT, service.REJECTED_OUTCOME) else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
