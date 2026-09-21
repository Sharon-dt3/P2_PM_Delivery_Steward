-- 0001_initial.sql
-- Core schema for P2 PM Delivery Steward's own seeded fixture (PM-01/02),
-- which PM-04's tracker/code-host/risk-log adapter mocks run over.
--
-- Mirrors P1's own migration conventions verbatim (see
-- ../P3_Agents/src/p1/storage/migrations/0001_initial.sql): TEXT primary
-- keys for externally-meaningful/human-assigned ids, INTEGER AUTOINCREMENT
-- for purely internal append-only rows, JSON stored as TEXT columns,
-- explicit REFERENCES, datetime('now')-style defaults where a row's own
-- timestamp isn't itself the seeded value.
--
-- items.status deliberately has NO CHECK constraint against a canonical
-- enum: PM-03's planted "free-text status outside the enum" difficulty
-- (see src/pm/seed/build.py) needs the schema itself to tolerate a status
-- value the tracker adapter's own CANONICAL_STATUSES doesn't recognise,
-- not just the application code.

CREATE TABLE IF NOT EXISTS sprints (
    id              TEXT PRIMARY KEY,      -- e.g. "sprint-12"
    display_name    TEXT NOT NULL,
    start_date      TEXT NOT NULL,         -- ISO date, inclusive
    end_date        TEXT NOT NULL          -- ISO date, inclusive
);

CREATE TABLE IF NOT EXISTS assignees (
    id              TEXT PRIMARY KEY,      -- same id space as P1's own members.id for the same person (see build.py's own docstring)
    display_name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id                  TEXT PRIMARY KEY,              -- e.g. "PM-014"
    title               TEXT NOT NULL,
    status              TEXT NOT NULL,                  -- backlog | in_progress | blocked | in_review | done -- OR free text (PM-03 edge case; see this file's own header comment)
    sprint_id           TEXT NOT NULL REFERENCES sprints(id),
    assignee_id         TEXT REFERENCES assignees(id),   -- NULL: unassigned item (PM-03 edge case)
    created_at          TEXT NOT NULL,                   -- ISO date; when this item entered the tracker
    blocked_since       TEXT,                            -- set only while status = 'blocked'; NULL otherwise
    source_message_id   TEXT                             -- optional: the P1 Teams message (proj-gamma fixture, reused not re-seeded) this item traces back to, when there is one
);

CREATE INDEX IF NOT EXISTS idx_items_sprint ON items(sprint_id);
CREATE INDEX IF NOT EXISTS idx_items_assignee ON items(assignee_id);

CREATE TABLE IF NOT EXISTS item_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL REFERENCES items(id),
    from_status     TEXT NOT NULL,
    to_status       TEXT NOT NULL,
    changed_at      TEXT NOT NULL          -- ISO date or datetime -- same-day churn (PM-03 edge case) needs the time component, most rows only need the date
);

CREATE INDEX IF NOT EXISTS idx_item_transitions_item ON item_transitions(item_id);

CREATE TABLE IF NOT EXISTS item_comments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL REFERENCES items(id),
    author_id       TEXT REFERENCES assignees(id),
    body            TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',   -- JSON list, e.g. ["blocked-reason"]
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_item_comments_item ON item_comments(item_id);

CREATE TABLE IF NOT EXISTS commits (
    sha             TEXT PRIMARY KEY,
    author_id       TEXT REFERENCES assignees(id),
    message         TEXT NOT NULL,
    item_ref        TEXT REFERENCES items(id),     -- NULL: commit with no item reference (PM-04 code-host edge case)
    committed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commits_item_ref ON commits(item_ref);

CREATE TABLE IF NOT EXISTS commitments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id           TEXT NOT NULL REFERENCES assignees(id),
    item_id             TEXT REFERENCES items(id),
    text                TEXT NOT NULL,
    due_date_iso        TEXT,               -- ISO date, when the commitment named one
    due_date_text       TEXT,               -- raw relative phrase (e.g. "end of week"), when it didn't (PM-03 edge case)
    made_at             TEXT NOT NULL,
    source_message_id   TEXT,
    CHECK (due_date_iso IS NOT NULL OR due_date_text IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS risks (
    id                  TEXT PRIMARY KEY,   -- e.g. "RISK-001"
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    severity            TEXT NOT NULL,      -- low | medium | high
    status              TEXT NOT NULL,      -- open | mitigated | closed
    related_item_id     TEXT REFERENCES items(id),   -- NULL: not tied to any single current item (PM-04 risk-log edge case: a pre-existing entry matching no current blocker)
    opened_at           TEXT NOT NULL,
    source               TEXT NOT NULL DEFAULT 'seed'
);
