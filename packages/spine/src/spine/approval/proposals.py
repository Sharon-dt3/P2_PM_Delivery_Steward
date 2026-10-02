"""
SPN-08: the proposal record and status machine (Shared).

"The core abstraction of all three agents. Build once, well." -- this
task's own rationale. Every outbound action any of the three agents
ever proposes -- a digest, a nudge, an escalation, a P2 tracker update,
a P3 backlog change -- becomes exactly one row here before anything is
ever sent anywhere. Nothing in this module sends, posts, or publishes
anything; that is SPN-09's write guard and each capability's own
publish adapter. This module only answers two questions: what did the
model actually propose, and what, if anything, is a human allowed to
do to that proposal next.

Two guarantees this record makes, both load-bearing for HITL gating
across all three agents:

1. original_model_output is captured once, at create(), and never
   written to again by anything in this module. payload starts equal
   to it but is the field a human's edit is allowed to replace, at
   approve() time. So "what the model said" and "what was actually
   approved and applied" are always two independently readable
   answers, never one overwriting the other -- this task's own
   acceptance test in one sentence.

2. status is a small explicit state machine, not a free-text field a
   caller can set to anything: pending can only become approved or
   rejected; approved can only become applied; rejected and applied
   are terminal. An illegal move (approving something already decided,
   applying something never approved) raises rather than silently
   succeeding -- the record-level half of approval gating. SPN-09 adds
   the service-layer half (refusing the underlying write itself, not
   just the status column), for the same reason SPN-06's grounding
   kernel and CHN-13's daily summary are two separate layers of the
   same guarantee rather than one doing both jobs.

create() is idempotent on idempotency_key the same way every other
store in this codebase upserts on reprocessing -- but unlike
DigestStore (where re-generating a not-yet-published digest safely
overwrites its content), a retried create() here must never reset an
already-decided proposal back to a fresh pending row, so it returns the
existing row untouched rather than overwriting anything. See
DECISION_LOG.md.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spine.storage.db import DEFAULT_DB_PATH, get_connection

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
APPLIED = "applied"

# Anything not listed as a legal destination for the current status is
# refused, not silently allowed -- pending is the only status with more
# than one legal next move, and rejected/applied are terminal.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({APPROVED, REJECTED}),
    APPROVED: frozenset({APPLIED}),
    REJECTED: frozenset(),
    APPLIED: frozenset(),
}


class ProposalNotFoundError(Exception):
    pass


class IllegalTransitionError(Exception):
    """Raised when a caller asks for a status move the state machine
    does not allow from the proposal's current status -- e.g. approving
    something already rejected, or applying something never approved."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Proposal:
    id: str
    type: str
    status: str
    payload: dict
    original_model_output: dict
    source_refs: tuple[str, ...]
    approver_id: str | None
    created_at: str
    decided_at: str | None
    idempotency_key: str


def _row_to_proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        type=row["type"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        original_model_output=json.loads(row["original_model_output"]),
        source_refs=tuple(json.loads(row["source_refs"])),
        approver_id=row["approver_id"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        idempotency_key=row["idempotency_key"],
    )


class ProposalStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    def create(
        self,
        *,
        type: str,
        payload: dict,
        original_model_output: dict,
        source_refs: list[str],
        idempotency_key: str,
    ) -> Proposal:
        existing = self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        proposal_id = uuid.uuid4().hex
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """
                INSERT INTO proposals (
                    id, type, status, payload, original_model_output, source_refs,
                    approver_id, created_at, decided_at, idempotency_key
                ) VALUES (:id, :type, :status, :payload, :original_model_output, :source_refs,
                          :approver_id, :created_at, :decided_at, :idempotency_key)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                {
                    "id": proposal_id,
                    "type": type,
                    "status": PENDING,
                    "payload": json.dumps(payload),
                    "original_model_output": json.dumps(original_model_output),
                    "source_refs": json.dumps(list(source_refs)),
                    "approver_id": None,
                    "created_at": _now(),
                    "decided_at": None,
                    "idempotency_key": idempotency_key,
                },
            )
            conn.commit()
        finally:
            conn.close()

        # A concurrent create() with the same key may have won the
        # ON CONFLICT DO NOTHING race -- either way, reading back by the
        # key returns whichever row actually exists now.
        return self.get_by_idempotency_key(idempotency_key)

    def refresh_payload(
        self, proposal_id: str, *, payload: dict, source_refs: list[str] | None = None,
        also_if_approved_by: str | None = None,
    ) -> Proposal:
        """Replaces a still-pending proposal's payload (and, when given,
        its source_refs) with freshly regenerated content -- used by
        daily_job.py so that a digest regenerated after this proposal
        was first created (new messages arriving during the day, before
        a human gets to it) is what actually gets approved and sent,
        never a stale snapshot frozen at creation time.
        original_model_output is never touched here, so "what the model
        originally said" stays readable no matter how many times
        payload is refreshed before a decision is made -- same
        guarantee create()'s own docstring already makes for that
        field, just re-affirmed on the update path.

        `also_if_approved_by` widens that to ONE more case: a proposal
        that is approved but not yet sent, where the approver is exactly
        that id. It exists for a system approver (the auto-approve id):
        no human vetted that wording, so when its send fails and is
        retried hours later the retry may carry current content instead
        of a stale snapshot -- 2026-09-24, a digest whose 17:30 send
        failed was resent at 20:57 with 17:30's content, missing two
        updates posted in between. A proposal a HUMAN approved never
        matches (their approval was of those exact words), and neither
        does one that was already sent.

        Raises IllegalTransitionError for anything else: once approved
        (by anyone else), rejected, or applied, payload is an
        honest record of what was actually decided and must never be
        silently rewritten out from under that decision -- discovered
        2026-09-19 when a real first-publish proposal sat pending for
        hours while new Teams messages arrived, and every rerun kept
        regenerating the digests table's own content while this
        proposal (the thing actually sent) stayed frozen at its
        creation-time snapshot. See DECISION_LOG.md.
        """
        current = self.get(proposal_id)
        refreshable = current.status == PENDING or (
            also_if_approved_by is not None
            and current.status == APPROVED
            and current.approver_id == also_if_approved_by
        )
        if not refreshable:
            raise IllegalTransitionError(
                f"proposal_id={current.id!r} is {current.status!r}; refusing to refresh its payload -- "
                "only a still-pending proposal's payload may be replaced with regenerated content"
            )
        new_source_refs = source_refs if source_refs is not None else list(current.source_refs)
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                "UPDATE proposals SET payload = ?, source_refs = ? WHERE id = ?",
                (json.dumps(payload), json.dumps(new_source_refs), proposal_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> Proposal:
        conn = get_connection(self._db_path)
        try:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ProposalNotFoundError(f"No proposal with id={proposal_id!r}")
        return _row_to_proposal(row)

    def get_by_idempotency_key(self, idempotency_key: str) -> Proposal | None:
        conn = get_connection(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM proposals WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_proposal(row) if row is not None else None

    def list_by_status(self, status: str) -> list[Proposal]:
        """Every proposal currently in `status`, oldest first -- the one
        query CHN-25's approval service (and, through it, both the
        Copilot Studio connector and the Streamlit fallback) uses to
        list what is awaiting a human decision. Deliberately a plain
        status filter, not scoped to any one capability's `type`, since
        a human approving nudges/escalations/first-publishes from one
        surface is this row's own point."""
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_proposal(row) for row in rows]

    def approve(self, proposal_id: str, *, approver_id: str, payload: dict | None = None) -> Proposal:
        """Moves a pending proposal to approved. payload, given only
        when a human edited the model's draft before approving it,
        replaces the proposal's own payload column -- original_model_output
        is never touched here, so what the model actually said stays
        readable no matter what a human changed it to."""
        return self._decide(proposal_id, to_status=APPROVED, approver_id=approver_id, payload=payload)

    def reject(self, proposal_id: str, *, approver_id: str) -> Proposal:
        return self._decide(proposal_id, to_status=REJECTED, approver_id=approver_id)

    def apply(self, proposal_id: str) -> Proposal:
        """Moves an approved proposal to applied -- recording that the
        approved payload was actually acted on (published, sent, a
        tracker item created, ...), never performing that action
        itself. approver_id and decided_at are left exactly as
        approve() set them: applying is a downstream fact about what
        happened next, not a second human decision."""
        current = self.get(proposal_id)
        self._require_legal(current, APPLIED)

        conn = get_connection(self._db_path)
        try:
            conn.execute("UPDATE proposals SET status = ? WHERE id = ?", (APPLIED, proposal_id))
            conn.commit()
        finally:
            conn.close()
        return self.get(proposal_id)

    def _decide(
        self,
        proposal_id: str,
        *,
        to_status: str,
        approver_id: str,
        payload: dict | None = None,
    ) -> Proposal:
        current = self.get(proposal_id)
        self._require_legal(current, to_status)

        new_payload = payload if payload is not None else current.payload
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                "UPDATE proposals SET status = ?, payload = ?, approver_id = ?, decided_at = ? WHERE id = ?",
                (to_status, json.dumps(new_payload), approver_id, _now(), proposal_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(proposal_id)

    @staticmethod
    def _require_legal(current: Proposal, to_status: str) -> None:
        legal = _LEGAL_TRANSITIONS.get(current.status, frozenset())
        if to_status not in legal:
            destinations = ", ".join(sorted(legal)) if legal else "none -- this status is terminal"
            raise IllegalTransitionError(
                f"proposal_id={current.id!r} is {current.status!r}; cannot move it to {to_status!r} "
                f"(legal next status(es): {destinations})"
            )
