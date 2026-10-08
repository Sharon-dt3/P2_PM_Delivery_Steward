"""The facts of a channel brief, computed in code from real Teams data, with nothing invented and no model involved.

Two real sources, and only these:

  P1's channel outcome record  one channel's one day: grounded evidence lines (updates, blockers, decisions, questions), each with the message
                               it came from, and who on the roster had no say that day. Read through the published contract
                               (pm.channel.record: the consent flag must be exactly true, the schema must match, or nothing is taken).
  P1's message store           who posted each message and when (the record names no author). Read-only. Optional: without it the lines
                               simply carry no author, they are never given a guessed one.

and what this agent itself did with those messages, found by the message ids the real record cites:

  tracker items   items whose source_message_id is one of the record's messages (created when a person approved a tracker batch)
  risk entries    entries whose text cites one of the record's messages (written when a person approved a risk batch)

Nothing here reads the seeded sample project (its people, its PM-0xx items, its sprints): a fact is in the brief only if a real message
or a real approval made it so. A line is quoted from P1's record exactly as P1's grounding verified it; this module never rewords one.

Which record: a morning brief for day D reports the latest record before D (what the team said last working day); an end-of-day summary for
day D reports D's own record, which P1 writes after its daily digest. A record older than MAX_AGE_DAYS is not used, because a stale brief
that looks current is worse than none.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.teams import P1_REPO_ROOT
from pm.adapters.tracker import TrackerMock
from pm.channel.record import ChannelRecord, RecordRefused, load_record, record_file
from pm.commitments.outcomes import is_commitment, resolve_due

MORNING, EVENING = "morning", "evening"
MAX_AGE_DAYS = 7  # a record older than this is not used
COMMITMENT_WINDOW_DAYS = 7  # promises are read from this many days of records
SECTIONS = ("blockers", "decisions", "updates", "questions")
SILENT_STATES = {"no_message": "No message", "posted_no_update": "Posted, no update"}  # "excluded" is not a person's absence

ENV_OUTCOMES_DIR = "PM_OUTCOMES_DIR"
ENV_P1_DB = "P1_DB_PATH"


class NoUsableRecord(Exception):
    """There is no record the brief may be made from. `code` is machine-readable, the message is for a person (and the log)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


def outcomes_root() -> Path:
    return Path(os.environ.get(ENV_OUTCOMES_DIR) or (P1_REPO_ROOT / "outcomes"))


def p1_db_path() -> Path:
    return Path(os.environ.get(ENV_P1_DB) or (P1_REPO_ROOT / "data" / "p1_live.db"))


@dataclass(frozen=True)
class Line:
    message_id: str
    text: str
    quote: str | None
    section: str
    author: str | None  # who said it, when P1's store knows; never guessed
    url: str | None = None  # the link to the exact Teams message, as P1 stored it; None when P1 has none


@dataclass(frozen=True)
class Promise:
    line: Line
    made_on: str  # the day it was said
    due_iso: str | None
    due_text: str | None
    state: str  # past_due | due_today | upcoming | no_date  -- never "done": nothing here can see whether it was done


@dataclass(frozen=True)
class Work:
    kind: str  # tracker | risk
    ref: str  # PM-031 | RISK-004
    label: str  # "blocked", "high"
    title: str
    message_id: str


@dataclass(frozen=True)
class ChannelBriefFacts:
    channel_id: str
    channel_name: str
    kind: str
    local_date: str
    record_date: str
    record_age_days: int
    sections: dict[str, tuple[Line, ...]]
    silent: tuple[tuple[str, str], ...]  # (who, what the record says: "No message" | "Posted, no update")
    promises: tuple[Promise, ...]
    work: tuple[Work, ...]
    sources: dict = field(default_factory=dict)  # what was read, for the audit

    def lines(self) -> list[Line]:
        return [line for name in SECTIONS for line in self.sections[name]]


# --- reading the real sources ------------------------------------------------------------------------------------------


def find_record(channel_id: str, newest: date, root: Path | None = None) -> tuple[Path, date] | None:
    """The newest outcome record for this channel dated `newest` or earlier, looking back at most MAX_AGE_DAYS. None when there is none."""
    root = root or outcomes_root()
    for ago in range(MAX_AGE_DAYS + 1):
        day = newest - timedelta(days=ago)
        path = record_file(root, channel_id, day)
        if path.exists():
            return path, day
    return None


def read_record(path: Path) -> ChannelRecord:
    try:
        return load_record(path)
    except RecordRefused as refusal:
        raise NoUsableRecord(refusal.code, refusal.reason) from refusal


class P1Directory:
    """Who posted what, and who is who, from P1's message store, opened read-only. Absent or unreadable: everything answers None."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else p1_db_path()
        self._names: dict[str, str] = {}
        self._messages: dict[str, tuple[str | None, str]] = {}
        self._links: dict[str, str] = {}
        self.available = False
        try:
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            for member_id, name in conn.execute("SELECT id, display_name FROM members"):
                self._names[member_id] = name or ""
            for message_id, author, posted_at in conn.execute("SELECT id, author_id, posted_at FROM messages"):
                self._messages[message_id] = (author, posted_at)
            try:  # a store from before permalinks existed has no such column: no links, everything else works
                self._links = {m: link for m, link in conn.execute("SELECT id, permalink FROM messages") if link}
            except sqlite3.Error:
                self._links = {}
            self.available = True
        except sqlite3.Error:
            self.available = False
        finally:
            conn.close()

    def name(self, member_id: str | None) -> str | None:
        """A person's name, or None. A name that is only their id (P1 registers one until it can learn the real one) is no name."""
        if not member_id:
            return None
        name = (self._names.get(member_id) or "").strip()
        return name if name and name != member_id and not re.fullmatch(r"[0-9a-f-]{30,}", name) else None

    def author_of(self, message_id: str) -> str | None:
        author, _ = self._messages.get(message_id, (None, ""))
        return self.name(author)

    def link_to(self, message_id: str) -> str | None:
        return self._links.get(message_id)

    def posted_on(self, message_id: str) -> str | None:
        _, posted_at = self._messages.get(message_id, (None, ""))
        return posted_at[:10] if posted_at else None


# --- what the agent did with those messages ----------------------------------------------------------------------------


def _work_from(message_ids: set[str], db_path) -> tuple[Work, ...]:
    if not message_ids:
        return ()
    work: list[Work] = []
    for item in TrackerMock(db_path=db_path).list_items():
        if item.source_message_id in message_ids:
            work.append(Work("tracker", item.id, item.status, item.title, item.source_message_id))
    for risk in RiskLogMock(db_path=db_path).list_risks():
        cited = next((m for m in sorted(message_ids) if f"message {m} " in risk.description or f"message {m}." in risk.description), None)
        if cited:
            work.append(Work("risk", risk.id, risk.severity, risk.title, cited))
    return tuple(work)


# --- the facts ---------------------------------------------------------------------------------------------------------------


def _promises(records: list[tuple[date, ChannelRecord]], directory: P1Directory, local_date: date) -> tuple[Promise, ...]:
    """Promises said in the records, with when they fall due. A promise is a line that reads as one (pm.commitments.outcomes.is_commitment).
    A dated one is shown only while it matters (past due, due today, due within the window); an undated one only from the newest record."""
    found: list[Promise] = []
    seen: set[tuple[str, str]] = set()
    newest = max((d for d, _ in records), default=None)
    for record_day, record in records:
        for section in ("updates", "decisions"):
            for item in getattr(record, section):
                key = (item.message_id, item.text)
                if key in seen or not is_commitment(item.text):
                    continue
                seen.add(key)
                made_on = directory.posted_on(item.message_id) or record.date
                due_iso, due_text = resolve_due(item.text, date.fromisoformat(made_on))
                line = Line(item.message_id, item.text, item.quote, section[:-1] if section != "updates" else "update", directory.author_of(item.message_id),
                            directory.link_to(item.message_id))
                if due_iso is None:
                    if record_day == newest:
                        found.append(Promise(line, made_on, None, None, "no_date"))
                    continue
                due = date.fromisoformat(due_iso)
                if due < local_date:
                    state = "past_due"
                elif due == local_date:
                    state = "due_today"
                elif due <= local_date + timedelta(days=COMMITMENT_WINDOW_DAYS):
                    state = "upcoming"
                else:
                    continue
                found.append(Promise(line, made_on, due_iso, due_text, state))
    order = {"past_due": 0, "due_today": 1, "upcoming": 2, "no_date": 3}
    return tuple(sorted(found, key=lambda p: (order[p.state], p.due_iso or "", p.line.message_id)))


def compute_channel_brief_facts(
    channel_id: str,
    channel_name: str,
    kind: str,
    local_date: date,
    *,
    db_path,
    outcomes_dir: Path | None = None,
    directory: P1Directory | None = None,
) -> ChannelBriefFacts:
    """The brief's facts for `channel_id` on `local_date`. Raises NoUsableRecord (with a code) when there is nothing real to report from."""
    root = outcomes_dir or outcomes_root()
    newest = local_date - timedelta(days=1) if kind == MORNING else local_date
    found = find_record(channel_id, newest, root)
    if found is None:
        raise NoUsableRecord("no_record", f"P1 has no outcome record for {channel_name} in the last {MAX_AGE_DAYS} days up to {newest}")
    path, record_day = found
    if kind == EVENING and record_day != local_date:
        raise NoUsableRecord("record_not_written_yet", f"P1's record for {local_date} is not written yet (the newest is {record_day})")
    record = read_record(path)
    directory = directory or P1Directory()

    sections = {
        name: tuple(Line(i.message_id, i.text, i.quote, name[:-1] if name != "updates" else "update", directory.author_of(i.message_id),
                         directory.link_to(i.message_id))
                    for i in getattr(record, name))
        for name in SECTIONS
    }
    silent = tuple(
        (directory.name(p.member_id) or "a rostered member", SILENT_STATES[p.state])
        for p in record.participation if p.state in SILENT_STATES
    )
    recent: list[tuple[date, ChannelRecord]] = []
    for ago in range(COMMITMENT_WINDOW_DAYS + 1):
        day = newest - timedelta(days=ago)
        recent_path = record_file(root, channel_id, day)
        if recent_path.exists():
            try:
                recent.append((day, load_record(recent_path)))
            except RecordRefused:
                continue  # a day P1 was not cleared to pass on contributes nothing, quietly, here: the refusal is already P1's to record
    message_ids = {i.message_id for name in SECTIONS for i in getattr(record, name)}
    return ChannelBriefFacts(
        channel_id=channel_id, channel_name=channel_name, kind=kind, local_date=local_date.isoformat(), record_date=record.date,
        record_age_days=(local_date - date.fromisoformat(record.date)).days, sections=sections, silent=silent,
        promises=_promises(recent, directory, local_date), work=_work_from(message_ids, db_path),
        sources={"record": str(path), "authors_from_p1_store": directory.available, "records_read_for_promises": [d.isoformat() for d, _ in recent]},
    )
