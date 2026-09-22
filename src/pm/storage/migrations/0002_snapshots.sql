-- 0002_snapshots.sql
-- PM-05's project-state snapshots: one row per taken_at, holding the
-- full normalised ProjectSnapshot (items, commits, channel messages) as
-- an opaque JSON payload.
--
-- taken_at is the externally-meaningful identity here (an ISO 8601 UTC
-- timestamp, this snapshot's own "when"), so it is the TEXT PRIMARY KEY
-- directly -- same convention 0001_initial.sql uses for sprints/items/
-- commits/risks -- rather than a separate surrogate id.
--
-- payload is not exploded into columns: a ProjectSnapshot's own shape
-- (items/commits/channel) already belongs to the tracker/code-host/
-- teams-reader adapters' own models (PM-04) -- this table's only job is
-- "persist it, keyed by when it was taken," not to own a second schema
-- for data those adapters already type.

CREATE TABLE IF NOT EXISTS snapshots (
    taken_at        TEXT PRIMARY KEY,   -- ISO 8601 UTC
    payload         TEXT NOT NULL       -- ProjectSnapshot, serialised as JSON
);
