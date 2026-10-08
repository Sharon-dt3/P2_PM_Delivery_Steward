"""The clock override both schedulers share: act as if it were a chosen wall-clock time."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def parse_at(text: str, tz_name: str) -> datetime:
    """A wall-clock time like 2026-09-16T08:00, read in the given timezone."""
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(timezone.utc)
