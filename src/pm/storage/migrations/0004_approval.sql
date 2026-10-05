-- PM-13: the approval gate's tables. Same shape as P1's (spine's ProposalStore
-- and write_guard read and write exactly these columns).
--
--   proposals  one row per thing the agent proposes to do. original_model_output
--              is written once and never changed; payload starts equal to it and
--              is what a human's edit replaces, so "what the agent proposed" and
--              "what was approved and applied" are always two readable answers.
--   write_log  one row per attempt to act on a proposal (sent / refused / failed).
--   audit      one row per human or agent decision about a proposal.
--
-- The deliveries table from 0003 is superseded: "one brief per channel per day"
-- is now the proposal's idempotency key, and nothing is delivered except through
-- an approved proposal.

CREATE TABLE IF NOT EXISTS proposals (
    id                     TEXT PRIMARY KEY,
    type                   TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | applied
    payload                TEXT NOT NULL,
    original_model_output  TEXT,
    source_refs            TEXT NOT NULL DEFAULT '[]',
    approver_id            TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at             TEXT,
    idempotency_key        TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS write_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id   TEXT REFERENCES proposals(id),
    action_type   TEXT NOT NULL,
    target        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    status        TEXT NOT NULL,      -- sent | refused | send_failed
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    details       TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_write_log_proposal ON write_log(proposal_id);

DROP TABLE IF EXISTS deliveries;
