"""Comparable moments from the mixed timestamp formats this system stores.

changed_at/created_at/committed_at values mix date-only strings
("2026-09-16", most seed rows) and full ISO timestamps
("2026-09-16T15:30:00", rows that need same-day ordering), with or without
an offset. Plain string ordering across that mix is not reliable, so every
time comparison in pm.state goes through real, timezone-aware datetimes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def parse_moment(value: str) -> datetime:
    """Parses a date-only or full ISO timestamp, offset or naive, into a
    timezone-aware datetime. A date-only value means 00:00 that day, and a
    naive value is treated as UTC, matching every writer in this system
    (TrackerMock._now_iso() and state.snapshot._now_iso() both use
    datetime.now(timezone.utc))."""
    if "T" in value:
        parsed = datetime.fromisoformat(value)
    else:
        parsed = datetime.combine(date.fromisoformat(value), datetime.min.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
