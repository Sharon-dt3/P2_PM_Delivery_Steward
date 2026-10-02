"""
Real proof of spine.approval (ProposalStore + guarded_send) standalone
-- the human-approval spine every outbound action in every agent built
on this package goes through.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from spine.approval.proposals import APPLIED, PENDING, REJECTED, ProposalStore
from spine.approval.write_guard import WriteRefusedError, guarded_send


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    # ProposalStore/guarded_send need the `proposals`/`write_log` tables;
    # this package ships no migration for them (that's each agent's own
    # schema decision, same as P1's committed storage/migrations/*.sql).
    # Build a minimal standalone schema here so this test proves the
    # CODE works independent of P1, without silently depending on P1's
    # own migration files.
    from spine.storage.db import get_connection

    conn = get_connection(path)
    conn.executescript(
        """
        CREATE TABLE proposals (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            original_model_output TEXT NOT NULL,
            source_refs TEXT NOT NULL,
            approver_id TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            idempotency_key TEXT UNIQUE
        );
        CREATE TABLE write_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            payload TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()
    return path


def test_pending_proposal_refuses_to_send(db_path: str):
    store = ProposalStore(db_path)
    proposal = store.create(
        type="test_action", payload={"msg": "hi"}, original_model_output={"msg": "hi"},
        source_refs=[], idempotency_key="key-1",
    )
    assert proposal.status == PENDING

    sent = []
    with pytest.raises(WriteRefusedError):
        guarded_send(
            proposal.id, action_type="test_action", target="someone",
            send_fn=lambda: sent.append(True), store=store, db_path=db_path,
        )
    assert sent == []  # never actually called


def test_approved_proposal_sends_exactly_once_even_if_guarded_send_called_twice(db_path: str):
    store = ProposalStore(db_path)
    proposal = store.create(
        type="test_action", payload={"msg": "hi"}, original_model_output={"msg": "hi"},
        source_refs=[], idempotency_key="key-2",
    )
    store.approve(proposal.id, approver_id="human-1")

    sent = []
    guarded_send(
        proposal.id, action_type="test_action", target="someone",
        send_fn=lambda: sent.append(1), store=store, db_path=db_path,
    )
    assert sent == [1]

    # second attempt -- already APPLIED, refuses again without re-sending
    with pytest.raises(WriteRefusedError):
        guarded_send(
            proposal.id, action_type="test_action", target="someone",
            send_fn=lambda: sent.append(2), store=store, db_path=db_path,
        )
    assert sent == [1]  # still exactly one real send

    final = store.get(proposal.id)
    assert final.status == APPLIED


def test_rejected_proposal_refuses_to_send(db_path: str):
    store = ProposalStore(db_path)
    proposal = store.create(
        type="test_action", payload={"msg": "hi"}, original_model_output={"msg": "hi"},
        source_refs=[], idempotency_key="key-3",
    )
    store.reject(proposal.id, approver_id="human-1")
    assert store.get(proposal.id).status == REJECTED

    with pytest.raises(WriteRefusedError):
        guarded_send(
            proposal.id, action_type="test_action", target="someone",
            send_fn=lambda: None, store=store, db_path=db_path,
        )
