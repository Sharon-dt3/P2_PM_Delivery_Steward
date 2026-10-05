"""
Project-state snapshot (PM-05, extended by PM-08 and PM-10).

Normalises tracker, code-host, channel, risk-log and commitments reads
into one persisted, timestamped record, so a later run can diff against
an earlier one, and so PM-08's morning brief can compute every one of its
facts from this one object alone ("the facts come from the snapshot" --
that row's own words). Nothing here calls a model -- this is purely
deterministic normalisation over already-typed adapter output (Tracker/
CodeHost/TeamsReader/RiskLogStore/CommitmentsStore, PM-04/PM-08's own
interfaces). Commits, channel messages, sprints, commitments, risks and
the roster already come back from their adapters in a shape with nothing
to normalise -- all six are held here unchanged, not re-modelled.

PM-10 adds `roster`, read via the tracker's own list_assignees() (no new
adapter, no new required parameter -- the same tracker argument already
passed in). It exists so PM-10's "not omitted" requirement can hold
without breaking compute_morning_brief_facts's own purity: the full set
of people to report on -- including anyone who owns no item, no
commitment and no commit -- comes from this one snapshot object too, the
same as everything else that function reads, rather than from a second,
out-of-band import of the seed's own roster constant.

The one real piece of normalisation is a tracker item's status:

  - a value in Tracker's own CANONICAL_STATUSES passes through as-is.
  - anything else is carried as the literal string "UNMAPPED" -- never
    coerced into a canonical bucket it doesn't belong to.

NormalizedItem.raw_status keeps the original value alongside `status`
either way, so "UNMAPPED" only ever *flags* an unrecognised status; it
never hides what that status actually was. PM-03's own planted
free-text-status difficulty (PM-022, "waiting_on_vendor") is exactly the
live case this exists for -- see test_state_snapshot.py.

sprints/commitments/risks default to an empty list on the model itself
(rather than being required) so an already-persisted PM-05-era snapshot
row -- built before PM-08 existed -- still deserialises via
pm.state.store.read_snapshot() without error; it just carries no data for
these three fields, which is the honest state of affairs for a snapshot
that genuinely predates them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from p1.adapters.teams_reader import TeamsMessage, TeamsReader
from pydantic import BaseModel

from pm.adapters.code_host import CodeHost, Commit
from pm.adapters.commitments import Commitment, CommitmentsStore
from pm.adapters.risk_log import Risk, RiskLogStore
from pm.adapters.tracker import Assignee, Sprint, Tracker, TrackerItem
from pm.seed.build import CANONICAL_STATUSES, CHANNEL_ID
from pm.state.identities import IdentityMap, load_identities
from pm.state.moments import parse_moment

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
    sprints: list[Sprint] = []
    commitments: list[Commitment] = []
    risks: list[Risk] = []
    roster: list[Assignee] = []
    identities: IdentityMap = IdentityMap()  # how commit authors map to people; see pm.state.identities


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


def _items_as_of(tracker: Tracker, items: list[TrackerItem], moment: datetime) -> list[NormalizedItem]:
    """Each item as it stood at `moment`, rebuilt from the tracker's
    transition history. An item created after `moment` did not exist yet
    and is left out. An item with no transition after `moment` passes
    through exactly as the tracker returns it -- which is every item, for
    a snapshot of the present. One that changed since is put back to the
    status it held then (before its first transition, that transition's own
    from_status), and carries blocked_since only if it was blocked then."""
    result = []
    for item in items:
        if parse_moment(item.created_at) > moment:
            continue
        history = tracker.list_transitions(item.id)
        if not any(parse_moment(record.changed_at) > moment for record in history):
            result.append(normalize_item(item))
            continue
        status = history[0].from_status
        entered_blocked = None
        for record in history:
            if parse_moment(record.changed_at) <= moment:
                status = record.to_status
                if record.to_status == "blocked":
                    entered_blocked = record.changed_at[:10]
        blocked_since = None
        if status == "blocked":
            blocked_since = entered_blocked
        result.append(normalize_item(item.model_copy(update={"status": status, "blocked_since": blocked_since})))
    return result


def build_snapshot(
    tracker: Tracker,
    code_host: CodeHost,
    teams_reader: TeamsReader,
    channel_id: str,
    *,
    risk_log: RiskLogStore,
    commitments_store: CommitmentsStore,
    taken_at: str | None = None,
    identities: IdentityMap | None = None,
) -> ProjectSnapshot:
    """Reads all five sources once and normalises them into one
    ProjectSnapshot. Pure normalisation over what the five adapters
    return -- this function performs no persistence of its own (see
    store.py for that) and calls no model.

    risk_log and commitments_store are keyword-only and required (not
    defaulted) so every call site stays fully explicit about which
    adapters it's reading from, the same posture tracker/code_host/
    teams_reader already take -- PM-08 added these two once its own
    facts needed them; build_current_snapshot() below is the one place
    that still offers no-argument convenience wiring.

    taken_at defaults to now (UTC, ISO 8601) if not given explicitly; two
    calls a moment apart naturally get two different values, which is
what lets two consecutive snapshots be told apart once persisted.

    A snapshot shows the project as it was at taken_at, not as it is now:
    items, commits, channel messages and commitments dated after taken_at
    are left out, and an item that changed since is rebuilt at the status
    it then held (see _items_as_of). For a snapshot of the present none of
    this changes anything. Not reconstructable, because the tracker keeps
    no history for them: an item's assignee and sprint, and a risk's status
    -- those are read as they are now, though a risk whose item did not yet
    exist is dropped."""
    taken_at = taken_at or _now_iso()
    moment = parse_moment(taken_at)
    items = _items_as_of(tracker, tracker.list_items(), moment)
    commits = [c for c in code_host.list_commits() if parse_moment(c.committed_at) <= moment]
    message_page = teams_reader.list_messages(channel_id)
    messages = [m for m in message_page.messages if parse_moment(m.posted_at) <= moment]
    channel = ChannelSnapshot(channel_id=channel_id, messages=messages)
    sprints = tracker.list_sprints()
    commitments = [c for c in commitments_store.list_commitments() if parse_moment(c.made_at) <= moment]
    existing_items = {item.id for item in items}
    risks = [r for r in risk_log.list_risks() if r.related_item_id is None or r.related_item_id in existing_items]
    roster = tracker.list_assignees()
    return ProjectSnapshot(
        taken_at=taken_at,
        items=items,
        commits=commits,
        channel=channel,
        sprints=sprints,
        commitments=commitments,
        risks=risks,
        roster=roster,
        identities=identities or IdentityMap(),
    )


def build_current_snapshot(
    db_path=None, *, taken_at: str | None = None, identities: IdentityMap | None = None
) -> ProjectSnapshot:
    """Convenience wiring for the common case: TrackerMock/CodeHostMock/
    RiskLogMock/CommitmentsMock over this repo's own seeded db, and P1's
    Teams reader over its own real fixture data
    (pm.adapters.teams.get_teams_reader) -- the same default-wiring role
    get_teams_reader()/get_teams_publisher() already play for the chat
    adapter itself. Callers who want different adapters (a future real
    tracker/code-host implementation, or a test double) should call
    build_snapshot() directly instead. identities defaults to the map in
    data/identities.json (empty when there is no such file)."""
    from pm.adapters.code_host import CodeHostMock
    from pm.adapters.commitments import CommitmentsMock
    from pm.adapters.risk_log import RiskLogMock
    from pm.adapters.teams import get_teams_reader
    from pm.adapters.tracker import TrackerMock
    from pm.storage.db import DEFAULT_DB_PATH

    resolved_db_path = db_path if db_path is not None else DEFAULT_DB_PATH
    tracker = TrackerMock(db_path=resolved_db_path)
    code_host = CodeHostMock(db_path=resolved_db_path)
    teams_reader = get_teams_reader(db_path=resolved_db_path)
    risk_log = RiskLogMock(db_path=resolved_db_path)
    commitments_store = CommitmentsMock(db_path=resolved_db_path)
    return build_snapshot(
        tracker,
        code_host,
        teams_reader,
        CHANNEL_ID,
        risk_log=risk_log,
        commitments_store=commitments_store,
        taken_at=taken_at,
        identities=identities if identities is not None else load_identities(),
    )
