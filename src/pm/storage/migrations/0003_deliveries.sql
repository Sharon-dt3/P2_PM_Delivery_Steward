-- PM-11: which scheduled briefs have already been handed to Teams. One row per
-- (kind, target channel, local date): the morning brief is delivered once per
-- channel per local day, so a restart, a misfire catch-up or a repeated run
-- cannot post it twice. A row is written only after a successful send, so a
-- failed delivery can be retried.

CREATE TABLE IF NOT EXISTS deliveries (
    kind            TEXT NOT NULL,      -- e.g. 'morning_brief'
    channel_id      TEXT NOT NULL,      -- the channel the brief was posted to
    local_date      TEXT NOT NULL,      -- the project's own calendar date, ISO
    delivered_at    TEXT NOT NULL,      -- ISO 8601 UTC
    PRIMARY KEY (kind, channel_id, local_date)
);
