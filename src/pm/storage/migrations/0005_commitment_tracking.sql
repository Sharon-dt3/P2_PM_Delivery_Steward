-- 0005_commitment_tracking.sql
-- PM-24: tracking a commitment from the day it is made to the day it is kept.
--
-- The seeded commitments table only said who promised what by when; whether it was kept was derived
-- from the item's tracker status. Commitments that come from channel outcome records often have no item,
-- so a commitment now carries its own state, and the follow-ups (a nudge before the due date, an overdue
-- record, an escalation to the lead) are recorded as events so each happens once.
--
-- `nudges` is this agent's side of the estate's shared nudge ledger: P1 keeps one of the same name and
-- shape (member_id, date, sent_at). The per-person per-day cap counts sent rows in BOTH.

ALTER TABLE commitments ADD COLUMN status TEXT NOT NULL DEFAULT 'open';   -- open | fulfilled | cancelled
ALTER TABLE commitments ADD COLUMN closed_at TEXT;                         -- ISO date it was closed
ALTER TABLE commitments ADD COLUMN source TEXT NOT NULL DEFAULT 'seed';    -- seed | outcome | manual

-- A message can hold one commitment once: re-reading the same outcome record never adds it twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_commitments_message_text
    ON commitments(source_message_id, text) WHERE source_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS commitment_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id   INTEGER NOT NULL REFERENCES commitments(id),
    kind            TEXT NOT NULL,        -- ingested | nudged | overdue | escalated | fulfilled | cancelled
    day             TEXT NOT NULL,        -- ISO local date it happened
    detail          TEXT NOT NULL DEFAULT '',
    proposal_id     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (commitment_id, kind)          -- each follow-up happens once per commitment
);

CREATE TABLE IF NOT EXISTS nudges (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id         TEXT NOT NULL,
    date              TEXT NOT NULL,      -- the local day the nudge belongs to
    commitment_id     INTEGER REFERENCES commitments(id),
    proposal_id       TEXT,
    sent_at           TEXT,               -- non-null only once it was actually delivered: only these count against the cap
    idempotency_key   TEXT NOT NULL UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nudges_member_date ON nudges(member_id, date);
