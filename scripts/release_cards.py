"""Put back approval cards the flow handed out but could not deliver (a failed Power Automate run), so the next claim sends them again.

    uv run python scripts/release_cards.py --db data/pm.db --reason "flow run failed" --all-pending
    uv run python scripts/release_cards.py --db data/pm.db --reason "..." PROPOSAL_ID [PROPOSAL_ID ...]

Writes `proposal.card_unsent` to the audit log; nothing is deleted and nothing is decided.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spine.approval.proposals import PENDING

from pm.approval.card_delivery import release_cards
from pm.storage.db import get_connection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("proposal_ids", nargs="*")
    parser.add_argument("--db", default="data/pm.db")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--all-pending", action="store_true", help="release every pending proposal that has a sent card")
    args = parser.parse_args()
    ids = list(args.proposal_ids)
    if args.all_pending:
        conn = get_connection(args.db)
        try:
            ids += [row[0] for row in conn.execute("SELECT id FROM proposals WHERE status = ?", (PENDING,))]
        finally:
            conn.close()
    if not ids:
        parser.error("give proposal ids or --all-pending")
    released = release_cards(ids, reason=args.reason, db_path=args.db)
    print(f"released {len(released)} of {len(ids)}: {', '.join(released) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
