"""
Shared calendar/timezone arithmetic -- the generic half of P1's
config/calendar.py (CHN-33 extraction).

parse_instant()/to_local() were already fully generic (plain strings and
ZoneInfo, no agent-specific type anywhere) and move here unchanged.

is_working_day()/local_datetime() originally took a concrete
p1.config.schema.ChannelConfig and p1.adapters.teams_reader.TeamsMessage
directly. Here they take structural Protocols instead --
WorkingCalendarConfig (anything with .working_days/.non_working_dates)
and TimestampedItem (anything with a single ISO-timestamp field) -- so
any agent's own config/message type satisfies them automatically, with
zero inheritance and zero change to that type, as long as the field
names line up. P1's ChannelConfig and TeamsMessage already have exactly
these fields, so P1's own call sites need no changes either -- this is
a pure widening of the accepted type, not a behavior change.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Matches the fractional-seconds group in an ISO 8601 timestamp, e.g.
# the ".35" in "2026-09-16T10:49:31.35+00:00".
_FRACTIONAL_SECONDS_RE = re.compile(r"\.(\d+)")


def parse_instant(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, tolerating two real shapes
    datetime.fromisoformat on Python 3.10 rejects outright: a trailing
    'Z' (UTC), and a fractional-seconds component whose length isn't
    exactly 3 or 6 digits (Microsoft Graph's own dateTimeOffset format
    uses 7; at least one message already ingested into a live P1
    database carries 2). Any digit count is normalized to 6
    (microseconds) -- datetime only stores microsecond precision
    anyway, so this loses nothing a caller could have kept."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    match = _FRACTIONAL_SECONDS_RE.search(value)
    if match:
        normalized = (match.group(1) + "000000")[:6]
        value = value[: match.start()] + "." + normalized + value[match.end() :]

    return datetime.fromisoformat(value)


def to_local(instant: str, timezone: str) -> datetime:
    """An ISO 8601 instant, converted into the given IANA timezone."""
    return parse_instant(instant).astimezone(ZoneInfo(timezone))


@runtime_checkable
class WorkingCalendarConfig(Protocol):
    working_days: list[str]
    non_working_dates: list[date]


@runtime_checkable
class TimestampedItem(Protocol):
    posted_at: str


@runtime_checkable
class TimezonedConfig(Protocol):
    timezone: str


def is_working_day(day: date, config: WorkingCalendarConfig) -> bool:
    """True iff `day` is one of config's recurring working weekdays and
    not individually excepted via non_working_dates (a one-off holiday
    on an otherwise-working weekday, say)."""
    if day in config.non_working_dates:
        return False
    return WEEKDAY_NAMES[day.weekday()] in config.working_days


def local_datetime(item: TimestampedItem, config: TimezonedConfig) -> datetime:
    """An item's own original timestamp (item.posted_at -- never an
    edited/updated timestamp; callers pick which field to pass),
    converted into config's own configured timezone. Callers must
    always reason about an item's day/time here, never in whatever
    offset the timestamp happened to arrive in."""
    return to_local(item.posted_at, config.timezone)
