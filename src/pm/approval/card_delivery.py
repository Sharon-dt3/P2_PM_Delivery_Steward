"""Which pending proposals still need a card in Teams: each one is handed out ONCE.

A flow that runs on a timer and posts a card for every pending proposal would post the same unanswered cards every cycle. Instead the flow asks
for the NEW ones (pm.api.copilot_studio_api /claim_new_approvals), and this module hands each proposal out once: when it does, it writes a
`proposal.card_sent` row to the audit log, in the same transaction that decided it was due, so two overlapping callers never get the same
proposal. A card the flow could not deliver is put back with `release_cards`, which records `proposal.card_unsent`: the proposal is then due a
first card again, and both rows stay in the audit. A proposal still undecided after PM_CARD_RESEND_HOURS (default 24) is handed out once more as a reminder; 0 turns reminders off.

It decides nothing about the proposal and sends nothing itself: the card is posted by the flow, the decision is the approval service's.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spine.approval.proposals import PENDING

from pm.approval import service
from pm.approval.audit import AGENT
from pm.storage.db import DEFAULT_DB_PATH, get_connection

CARD_SENT = "proposal.card_sent"
CARD_UNSENT = "proposal.card_unsent"
ENV_RESEND_HOURS = "PM_CARD_RESEND_HOURS"
DEFAULT_RESEND_HOURS = 24


def resend_after() -> timedelta | None:
    raw = (os.environ.get(ENV_RESEND_HOURS) or "").strip()
    hours = float(raw) if raw.replace(".", "", 1).isdigit() else DEFAULT_RESEND_HOURS
    return timedelta(hours=hours) if hours > 0 else None


def _last_sent(conn) -> dict[str, str]:
    """When each proposal's card last went out; a proposal whose latest card record is `card_unsent` has none."""
    latest: dict[str, tuple[int, str, str]] = {}
    for entity_id, action, created_at, row_id in conn.execute(
        "SELECT entity_id, action, created_at, id FROM audit WHERE action IN (?, ?) AND entity_type = 'proposal' ORDER BY id", (CARD_SENT, CARD_UNSENT)
    ):
        latest[entity_id] = (row_id, action, created_at)
    return {entity_id: created_at for entity_id, (_, action, created_at) in latest.items() if action == CARD_SENT}


def release_cards(proposal_ids: list[str], *, reason: str, db_path: str | Path = DEFAULT_DB_PATH, now: datetime | None = None) -> list[str]:
    """Record that the card for each of these still-pending proposals was handed out but never reached Teams, so the next claim hands it out again.
    Returns the ids released; a proposal that is decided, unknown, or has no sent card is left alone."""
    moment = (now or datetime.now(timezone.utc)).isoformat()
    conn = get_connection(db_path)
    released: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending = {row[0] for row in conn.execute("SELECT id FROM proposals WHERE status = ?", (PENDING,))}
        sent = _last_sent(conn)
        for proposal_id in proposal_ids:
            if proposal_id in pending and proposal_id in sent and proposal_id not in released:
                conn.execute(
                    "INSERT INTO audit (actor, action, entity_type, entity_id, details, created_at) VALUES (?, ?, 'proposal', ?, ?, ?)",
                    (AGENT, CARD_UNSENT, proposal_id, json.dumps({"reason": reason}), moment),
                )
                released.append(proposal_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return released


def claim_new_approvals(*, db_path: str | Path = DEFAULT_DB_PATH, now: datetime | None = None) -> list[service.PendingApproval]:
    """The pending proposals that are due a card (never had one, or the last one is older than the resend window), oldest first, each now
    marked as handed out. Returns them as the approval service describes them."""
    moment = now or datetime.now(timezone.utc)
    window = resend_after()
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")  # one caller at a time decides what is due and records it
        pending = [row[0] for row in conn.execute("SELECT id FROM proposals WHERE status = ? ORDER BY created_at, rowid", (PENDING,))]
        last_sent = _last_sent(conn)
        due: list[tuple[str, str]] = []
        for proposal_id in pending:
            sent = last_sent.get(proposal_id)
            if sent is None:
                due.append((proposal_id, "first"))
            elif window is not None and moment - datetime.fromisoformat(sent) >= window:
                due.append((proposal_id, "reminder"))
        for proposal_id, kind in due:
            conn.execute(
                "INSERT INTO audit (actor, action, entity_type, entity_id, details, created_at) VALUES (?, ?, 'proposal', ?, ?, ?)",
                (AGENT, CARD_SENT, proposal_id, json.dumps({"kind": kind}), moment.isoformat()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    wanted = {proposal_id for proposal_id, _ in due}
    return [p for p in service.list_pending_approvals(db_path=db_path) if p.proposal_id in wanted]
