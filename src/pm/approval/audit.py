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

RISK_WRITE_TYPES = frozenset({"risk_log_entry", "channel_risk_entries"})  # the proposal types approving which writes to the risk log
TRACKER_WRITE_TYPES = frozenset({"channel_tracker_changes"})  # ...and the one that writes to the tracker

AGENT = "agent"  # the actor recorded for things the system itself does
AUTO_APPROVER = "system:auto-approve"  # the actor recorded when the system approves (see pm.approval.service)


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
        # edited only means something for text a person can change; a risk-log proposal has no text of its own to edit
        edited="content" in proposal.original_model_output
        and proposal.original_model_output["content"] != proposal.payload.get("content"),
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
    elif trail.approver_id == AUTO_APPROVER:
        approval = next((e for e in trail.events if e["action"] == "proposal.approved"), None)
        reason = (approval or {}).get("details", {}).get("reason", "")
        lines.append(
            f"Automatically approved by the system at {trail.decided_at}; no person reviewed it"
            + (f" ({reason})." if reason else ".")
        )
    else:
        edited = " with edits" if trail.edited else " as proposed"
        lines.append(f"Approved by {trail.approver_id} at {trail.decided_at}{edited}.")
    if trail.status in (APPROVED, APPLIED):
        writes_risk_log = trail.type in RISK_WRITE_TYPES
        if trail.sent and trail.type in TRACKER_WRITE_TYPES:
            applied = next((e for e in reversed(trail.events) if e["action"] == "proposal.applied"), None)
            done = (applied or {}).get("details", {})
            what = "; ".join(x for x in (
                f"created {', '.join(done['created'])}" if done.get("created") else "",
                f"commented on {', '.join(sorted(set(done['commented'])))}" if done.get("commented") else "",
                f"{len(done['skipped'])} skipped (already there or not writable)" if done.get("skipped") else "") if x)
            lines.append(f"Written to the tracker at {trail.sent['created_at']}: {what or trail.sent['target']}.")
        elif trail.sent and writes_risk_log:
            applied = next((e for e in reversed(trail.events) if e["action"] == "proposal.applied"), None)
            rating = (applied or {}).get("details", {})
            how = (f" Severity {rating['severity']} ({'chosen by the approver' if rating.get('severity_source') == 'approver' else 'the default: nobody rated it'})."
                   if rating.get("severity") else "")
            lines.append(f"Written to the risk log as {trail.sent['target']} at {trail.sent['created_at']}.{how}")
        elif trail.sent:
            lines.append(f"Sent to {trail.sent['target']} at {trail.sent['created_at']}.")
        elif trail.type in TRACKER_WRITE_TYPES:
            lines.append("Not written to the tracker yet.")
        else:
            lines.append("Not written to the risk log yet." if writes_risk_log else "Not sent yet.")
    lines += ["", "The agent originally proposed:", original]
    if trail.edited:
        lines += ["", "What was applied (edited):", trail.final_proposal.get("content", "")]
    return "\n".join(lines)


@dataclass(frozen=True)
class ProposalRow:
    proposal_id: str
    status: str
    created_at: str
    local_date: str
    target_channel: str
    approver_id: str | None
    decided_at: str | None


def recent_proposals(*, limit: int = 20, db_path: str | Path = DEFAULT_DB_PATH) -> list[ProposalRow]:
    """The latest proposals in any status, newest first -- what an audit view lists."""
    conn = get_connection(db_path)
    try:
        rows = _rows(
            conn,
            "SELECT id, status, created_at, payload, approver_id, decided_at FROM proposals "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            limit,
        )
    finally:
        conn.close()
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        out.append(
            ProposalRow(
                proposal_id=row["id"], status=row["status"], created_at=row["created_at"],
                local_date=payload.get("local_date", "?"), target_channel=payload.get("target_channel", "?"),
                approver_id=row["approver_id"], decided_at=row["decided_at"],
            )
        )
    return out
