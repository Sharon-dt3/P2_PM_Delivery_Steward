"""
Project-state snapshot (PM-05).

Normalises tracker, code-host and channel reads into one persisted,
timestamped record, so a later run can diff against an earlier one.
Nothing here calls a model -- this is purely deterministic normalisation
over already-typed adapter output (Tracker/CodeHost/TeamsReader, PM-04's
own interfaces). Commits and channel messages already come back from
their adapters in a shape with nothing to normalise (a commit's
`item_ref` is either a real item id or None; a TeamsMessage is already
one fixed shape) -- both are held here unchanged, not re-modelled.

The one real piece of normalisation is a tracker item's status:

  - a value in Tracker's own CANONICAL_STATUSES passes through as-is.
  - anything else is carried as the literal string "UNMAPPED" -- never
    coerced into a canonical bucket it doesn't belong to.

NormalizedItem.raw_status keeps the original value alongside `status`
either way, so "UNMAPPED" only ever *flags* an unrecognised status; it
never hides what that status actually was. PM-03's own planted
free-text-status difficulty (PM-022, "waiting_on_vendor") is exactly the
live case this exists for -- see test_state_snapshot.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from p1.adapters.teams_reader import TeamsMessage, TeamsReader
from pydantic import BaseModel

from pm.adapters.code_host import CodeHost, Commit
from pm.adapters.tracker import Tracker, TrackerItem
from pm.seed.build import CANONICAL_STATUSES, CHANNEL_ID

UNMAPPED = "UNMAPPED"


class NormalizedItem(BaseModel):
    id: str
    title: str
    status: str  # a CANONICAL_STATUSES value, or UNMAPPED -- see module docstring
    raw_status: str  # the tracker's own literal value, always preserved
    sprint_id: str
    assignee_id: str | None = None
    created_at: str
    blocked_since: str | None = None
    source_message_id: str | None = None


class ChannelSnapshot(BaseModel):
    channel_id: str
    messages: list[TeamsMessage]


class ProjectSnapshot(BaseModel):
    taken_at: str  # ISO 8601 UTC -- this snapshot's own identity and sort key
    items: list[NormalizedItem]
    commits: list[Commit]
    channel: ChannelSnapshot


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_item(item: TrackerItem) -> NormalizedItem:
    """The module's one normalisation rule, applied to a single tracker
    item: pass a canonical status through unchanged; anything else
    becomes UNMAPPED, with the original preserved in raw_status."""
    status = item.status if item.status in CANONICAL_STATUSES else UNMAPPED
    return NormalizedItem(
        id=item.id,
        title=item.title,
        status=status,
        raw_status=item.status,
        sprint_id=item.sprint_id,
        assignee_id=item.assignee_id,
        created_at=item.created_at,
        blocked_since=item.blocked_since,
        source_message_id=item.source_message_id,
    )


def build_snapshot(
    tracker: Tracker,
    code_host: CodeHost,
    teams_reader: TeamsReader,
    channel_id: str,
    *,
    taken_at: str | None = None,
) -> ProjectSnapshot:
    """Reads all three sources once and normalises them into one
    ProjectSnapshot. Pure normalisation over what the three adapters
    return -- this function performs no persistence of its own (see
    store.py for that) and calls no model.

    taken_at defaults to now (UTC, ISO 8601) if not given explicitly; two
    calls a moment apart naturally get two different values, which is
    what lets two consecutive snapshots be told apart once persisted."""
    taken_at = taken_at or _now_iso()
    items = [normalize_item(item) for item in tracker.list_items()]
    commits = code_host.list_commits()
    message_page = teams_reader.list_messages(channel_id)
    channel = ChannelSnapshot(channel_id=channel_id, messages=message_page.messages)
    return ProjectSnapshot(taken_at=taken_at, items=items, commits=commits, channel=channel)


def build_current_snapshot(db_path=None, *, taken_at: str | None = None) -> ProjectSnapshot:
    """Convenience wiring for the common case: TrackerMock/CodeHostMock
    over this repo's own seeded db, and P1's Teams reader over its own
    real fixture data (pm.adapters.teams.get_teams_reader) -- the same
    default-wiring role get_teams_reader()/get_teams_publisher() already
    play for the chat adapter itself. Callers who want different
    adapters (a future real tracker/code-host implementation, or a test
    double) should call build_snapshot() directly instead."""
    from pm.adapters.code_host import CodeHostMock
    from pm.adapters.teams import get_teams_reader
    from pm.adapters.tracker import TrackerMock
    from pm.storage.db import DEFAULT_DB_PATH

    resolved_db_path = db_path if db_path is not None else DEFAULT_DB_PATH
    tracker = TrackerMock(db_path=resolved_db_path)
    code_host = CodeHostMock(db_path=resolved_db_path)
    teams_reader = get_teams_reader(db_path=resolved_db_path)
    return build_snapshot(tracker, code_host, teams_reader, CHANNEL_ID, taken_at=taken_at)
