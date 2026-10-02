-- spine.config.store's own generic tables. Filename is prefixed
-- "spine_" deliberately, since this runs through the same
-- schema_migrations bookkeeping (keyed on filename) as whatever
-- agent-specific migrations already apply to the same db_path --
-- the prefix keeps the two migration sets from ever colliding.

CREATE TABLE IF NOT EXISTS spine_config_store (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spine_config_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spine_config_audit_config_id ON spine_config_audit(config_id);
