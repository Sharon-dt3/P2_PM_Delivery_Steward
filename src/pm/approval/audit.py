"""The audit trail: who decided about a proposal, when, and what the agent
originally proposed versus what was finally applied.

Everything here is read straight from the three tables the gate writes
(proposals, audit, write_log); nothing is kept in memory or re-derived from
anything else, so the answer is the same however the decision was made (card,
CLI, or a direct service call).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spine.approval.proposals import APPLIED, APPROVED, PENDING, REJECTED, ProposalStore

from pm.storage.db import DEFAULT_DB_PATH, get_connection

AGENT = "agent"  # the actor recorded for things the system itself does


def write_audit(
    db_path: str | Path, *, actor: str, action: str, proposal_id: str, details: dict | None = None
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO audit (actor, action, entity_type, entity_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (actor, action, "proposal", proposal_id, json.dumps(details or {}), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass(frozen=True)
class AuditTrail:
    proposal_id: str
    type: str
    status: str
    created_at: str
    original_proposal: dict  # what the agent proposed; never overwritten
    final_proposal: dict  # what was approved (and applied, once sent)
    edited: bool
    approver_id: str | None
    decided_at: str | None
    sent: dict | None  # the successful send, if any
    send_attempts: list[dict]
    events: list[dict]  # every recorded decision, oldest first


def _iso_utc(stamp: str) -> str:
    """SQLite's default timestamps ("2026-10-05 11:32:31", UTC, no zone) as the
    same ISO 8601 form every other timestamp here uses."""
    if "T" in stamp:
        return stamp
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).isoformat()


def _rows(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def audit_trail(proposal_id: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> AuditTrail:
    """Raises spine's ProposalNotFoundError for an unknown id."""
    proposal = ProposalStore(db_path).get(proposal_id)
    conn = get_connection(db_path)
    try:
        events = [
            {**row, "details": json.loads(row["details"])}
            for row in _rows(
                conn,
                "SELECT actor, action, details, created_at FROM audit "
                "WHERE entity_type = 'proposal' AND entity_id = ? ORDER BY id",
                proposal_id,
            )
        ]
        attempts = [
            {**row, "payload": json.loads(row["payload"]), "created_at": _iso_utc(row["created_at"])}
            for row in _rows(
                conn,
                "SELECT action_type, target, status, payload, created_at FROM write_log WHERE proposal_id = ? ORDER BY id",
                proposal_id,
            )
        ]
    finally:
        conn.close()
    sent = next((a for a in reversed(attempts) if a["status"] == "sent"), None)
    return AuditTrail(
        proposal_id=proposal.id,
        type=proposal.type,
        status=proposal.status,
        created_at=proposal.created_at,
        original_proposal=proposal.original_model_output,
        final_proposal=proposal.payload,
        edited=proposal.original_model_output.get("content") != proposal.payload.get("content"),
        approver_id=proposal.approver_id,
        decided_at=proposal.decided_at,
        sent=sent,
        send_attempts=attempts,
        events=events,
    )


def describe(trail: AuditTrail) -> str:
    """The trail as plain text: who decided, when, what was proposed, what was applied."""
    original = trail.original_proposal.get("content", "")
    lines = [f"Proposal {trail.proposal_id} ({trail.type}), created {trail.created_at}."]
    if trail.status == PENDING:
        lines.append("Awaiting a decision: nobody has approved or rejected it yet.")
    elif trail.status == REJECTED:
        lines.append(f"Rejected by {trail.approver_id} at {trail.decided_at}.")
    else:
        edited = " with edits" if trail.edited else " as proposed"
        lines.append(f"Approved by {trail.approver_id} at {trail.decided_at}{edited}.")
    if trail.status in (APPROVED, APPLIED):
        if trail.sent:
            lines.append(f"Sent to {trail.sent['target']} at {trail.sent['created_at']}.")
        else:
            lines.append("Not sent yet.")
    lines += ["", "The agent originally proposed:", original]
    if trail.edited:
        lines += ["", "What was applied (edited):", trail.final_proposal.get("content", "")]
    return "\n".join(lines)
