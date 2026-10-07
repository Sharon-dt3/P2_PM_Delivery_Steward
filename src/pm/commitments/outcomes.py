"""Feeding the commitment store from P1's channel outcome records (CHN-26: "the contract the P2 PM agent consumes").

An outcome record is one channel's one day: grounded evidence lines (updates, blockers, decisions, questions), each
with the message it came from. A commitment is a promise to do something, usually by a date ("I'll have the export bug
fixed by end of week"); it shows up in the updates and decisions. The rules here are plain Python, no model:

- which lines are commitments (a promise marker, not a status report: "Pushed the fix, should resolve the flaky
  test" is not one);
- when each is due, resolved against the day it was said ("tomorrow", "end of week", "by Friday", an ISO date);
- who made it: the author of the message the line cites, looked up in the channel's messages (an outcome record names
  no author); a line whose author is unknown or not on the project roster is not added, and is reported as such;
- the record's consent flag: `allowlisted: false` means P1 was not cleared to pass the day's content on, so nothing
  is taken from it.

Reading the same record again adds nothing: a commitment is keyed by the message it came from and its text.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from p1.contracts.outcome_record import OutcomeRecord, read_outcome

from pm.commitments.store import INGESTED, CommitmentTracker

ADDED, ALREADY_KNOWN = "added", "already_known"
NOT_A_COMMITMENT, NO_AUTHOR, NOT_ON_ROSTER, CONSENT_WITHHELD = "not_a_commitment", "no_author", "not_on_roster", "consent_withheld"

# A promise to do something: first person future, or "will have / should land / done by". Not a report of what was done.
_PROMISE = re.compile(
    r"\b(i'?ll|i will|we'?ll|we will|will (?:have|be|land|ship|deliver|send|fix|finish|get|push|merge|update)|"
    r"should (?:land|be in|be done|be ready|ship|have)|going to|plan(?:ning)? to|expect(?:s|ed)? to|"
    r"(?:done|ready|fixed|in|delivered) by)\b",
    re.IGNORECASE,
)
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY = re.compile(r"\b(?:by|on|before|until)\s+(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE)
_END_OF_WEEK = re.compile(r"\b(?:end of (?:the )?week|eow)\b", re.IGNORECASE)
_TODAY = re.compile(r"\b(?:end of (?:the )?day|eod|today|tonight)\b", re.IGNORECASE)
_TOMORROW = re.compile(r"\btomorrow\b", re.IGNORECASE)
_ITEM = re.compile(r"\bPM-\d+\b")


def is_commitment(text: str) -> bool:
    return bool(_PROMISE.search(text))


def resolve_due(text: str, made_on: date) -> tuple[str | None, str | None]:
    """(due date as ISO, the phrase it was read from). Relative phrases are resolved against the day it was said;
    a phrase that names no day gives (None, None). The arithmetic is plain calendar arithmetic."""
    iso = _ISO.search(text)
    if iso:
        return iso.group(1), iso.group(1)
    if _TOMORROW.search(text):
        return (made_on + timedelta(days=1)).isoformat(), "tomorrow"
    if _END_OF_WEEK.search(text):
        friday = made_on + timedelta(days=(4 - made_on.weekday()) % 7)  # this Friday, or next if it is the weekend
        return friday.isoformat(), "end of week"
    if _TODAY.search(text):
        return made_on.isoformat(), "today"
    weekday = _WEEKDAY.search(text)
    if weekday:
        ahead = (_WEEKDAYS.index(weekday.group(1).lower()) - made_on.weekday()) % 7 or 7  # the next one, never today
        return (made_on + timedelta(days=ahead)).isoformat(), weekday.group(0).lower()
    return None, None


@dataclass(frozen=True)
class MessageInfo:
    author_id: str | None
    posted_at: str  # ISO timestamp or date


@dataclass(frozen=True)
class IngestResult:
    message_id: str | None
    status: str
    commitment_id: int | None = None
    detail: str = ""


def ingest_outcome_record(
    record: OutcomeRecord,
    *,
    tracker: CommitmentTracker,
    message_info: Callable[[str], MessageInfo | None],
    roster: set[str],
    item_exists: Callable[[str], bool] = lambda _id: True,
) -> list[IngestResult]:
    """Add each commitment in the record's updates and decisions to the store. Returns what happened to every line
    that was looked at, so nothing is silently skipped."""
    if not record.allowlisted:
        return [IngestResult(None, CONSENT_WITHHELD, detail=f"{record.channel_id} {record.date}: P1 was not cleared to pass this day's content on")]

    results: list[IngestResult] = []
    for line in [*record.updates, *record.decisions]:
        if not is_commitment(line.text):
            results.append(IngestResult(line.message_id, NOT_A_COMMITMENT))
            continue
        info = message_info(line.message_id)
        if info is None or not info.author_id:
            results.append(IngestResult(line.message_id, NO_AUTHOR, detail="the message's author is not known"))
            continue
        if info.author_id not in roster:
            results.append(IngestResult(line.message_id, NOT_ON_ROSTER, detail=f"{info.author_id} is not on the project roster"))
            continue
        made_on = date.fromisoformat(info.posted_at[:10])
        due_iso, due_phrase = resolve_due(line.text, made_on)
        item = next((m for m in _ITEM.findall(line.text) if item_exists(m)), None)
        commitment, created = tracker.add(
            member_id=info.author_id, text=line.text, made_at=made_on.isoformat(), item_id=item, due_date_iso=due_iso,
            due_date_text=due_phrase, source_message_id=line.message_id, source="outcome",
        )
        if created:
            tracker.record_event(commitment.id, INGESTED, record.date.isoformat(), f"from the outcome record for {record.date}")
        results.append(IngestResult(line.message_id, ADDED if created else ALREADY_KNOWN, commitment.id))
    return results


def load_outcome_file(path: str | Path) -> OutcomeRecord:
    """A record read back through the same model that wrote it (a malformed or hand-edited file fails loudly)."""
    return OutcomeRecord.model_validate_json(Path(path).read_text())


def message_lookup(channel_id: str, db_path: str | Path) -> Callable[[str], MessageInfo | None]:
    """Who posted each message and when, from this project's own Teams reader (P1's, unchanged). An outcome record
    names the message a line came from but not its author: this is where the author is found."""
    from pm.adapters.teams import get_teams_reader

    messages = get_teams_reader(db_path=db_path).list_messages(channel_id).messages
    by_id = {m.id: MessageInfo(getattr(m, "author_id", None), m.posted_at) for m in messages}
    return by_id.get


def ingest_recent_outcomes(
    *,
    channel_id: str,
    today: date,
    days_back: int,
    output_dir: str | Path,
    tracker: CommitmentTracker,
    message_info: Callable[[str], MessageInfo | None],
    roster: set[str],
    item_exists: Callable[[str], bool] = lambda _id: True,
) -> list[IngestResult]:
    """Read each of the last `days_back` days' outcome records (P1 writes one per channel per day), skipping a day with
    no record, and add what they hold. Reading a day twice adds nothing, so the window can overlap from run to run."""
    results: list[IngestResult] = []
    for ago in range(days_back, -1, -1):
        try:
            record = read_outcome(channel_id, today - timedelta(days=ago), output_dir=output_dir)
        except FileNotFoundError:
            continue
        results += ingest_outcome_record(record, tracker=tracker, message_info=message_info, roster=roster, item_exists=item_exists)
    return results
