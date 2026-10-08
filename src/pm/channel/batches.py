"""PM-26: P1's channel outcome record becomes two batched proposal sets, each item carrying the channel and the message that justify it.

The record's lines are already grounded by P1 (each cites the message it came from, many with a verbatim quote). This module
adds no model: which tracker item a line is about, and what to propose, are plain rules.

  tracker batch   a line (update, decision, blocker or question) naming a tracker item that exists  -> a comment on that item,
                    tagged with where it came from
                  a blocker naming no tracker item at all                                          -> a new item to track it
                    (status blocked, nobody assigned: an owner is never guessed; the sprint is the one that covers the day)
  risk batch      a blocker naming an item that has no OPEN risk-log entry  -> a risk entry for it (suggested owner only
                    where the tracker has an assignee; no severity: the lead sets it)
                  a blocker naming no tracker item                          -> a risk entry with no item
  neither         a line naming an item the tracker does not have (never guessed at), or an update, decision or question
                    naming none: skipped, with the reason, so nothing disappears silently

Each item is {kind, ..., reference, fingerprint, source_text}. P1 splits a long message into several lines; the lines of one
message in one section are one item here, since the reference is the message. `reference` is the channel (id and name), the day, the message id
and the verbatim quote P1 gave, and `ref` = "teams:<channel_id>:<message_id>", which is also what the proposal lists in its
source_refs. `fingerprint` identifies the item (set, kind, channel, message, item and the line's words): it is how a record read
twice, or regenerated later in the day, adds only what is new, and how a rejected item is not proposed again. A line whose wording
changed is a new item, and says which earlier proposal and wording it replaces.

Nothing here writes to the tracker or the risk log. The two proposals are decided by a person like every other. Approving the risk
batch writes its entries to the risk log (pm.approval.risk_apply); approving the tracker batch writes its items and
comments to the tracker (pm.approval.tracker_apply).

A record that was not cleared (the scope/consent flag not true), or is not the published schema, is refused by pm.channel.record
before anything is planned: zero proposals, and one row in the audit log saying why.

This module imports nothing of P1's; neither does pm.channel.record.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from spine.approval.proposals import APPLIED, APPROVED, PENDING, REJECTED, ProposalStore

from pm.approval.audit import AGENT, write_audit
from pm.channel.gate import BATCHES, log_refusal
from pm.channel.record import ChannelRecord, Evidence, RecordRefused, load_record
from pm.storage.db import DEFAULT_DB_PATH

CHANNEL_TRACKER_PROPOSAL_TYPE = "channel_tracker_changes"
CHANNEL_RISK_PROPOSAL_TYPE = "channel_risk_entries"
TRACKER_SET, RISK_SET = "tracker", "risk"
_ITEM_ID = re.compile(r"\bPM-\d+\b")
_TITLE_CHARS = 100


# --- what the tracker and the risk log say right now ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemFacts:
    status: str
    assignee_id: str | None


@dataclass(frozen=True)
class RiskFacts:
    id: str
    related_item_id: str | None
    status: str


@dataclass(frozen=True)
class SprintFacts:
    id: str
    start_date: str
    end_date: str


@dataclass(frozen=True)
class TrackerView:
    """The little the planner needs to know: which items exist and who owns them, which risks are open, which sprint covers a day."""

    items: Mapping[str, ItemFacts]
    risks: tuple[RiskFacts, ...] = ()
    sprints: tuple[SprintFacts, ...] = ()

    @classmethod
    def from_adapters(cls, tracker, risk_log) -> TrackerView:
        return cls(
            items={i.id: ItemFacts(i.status, i.assignee_id) for i in tracker.list_items()},
            risks=tuple(RiskFacts(r.id, r.related_item_id, r.status) for r in risk_log.list_risks()),
            sprints=tuple(SprintFacts(s.id, s.start_date, s.end_date) for s in tracker.list_sprints()),
        )

    def sprint_covering(self, day: str) -> str | None:
        covering = sorted(s.id for s in self.sprints if s.start_date <= day <= s.end_date)
        return covering[0] if covering else None

    def open_risk_for(self, item_id: str) -> RiskFacts | None:
        return next((r for r in self.risks if r.related_item_id == item_id and r.status == "open"), None)


# --- the plan ----------------------------------------------------------------------------------------------------------------------


@dataclass
class Plan:
    tracker_items: list[dict] = field(default_factory=list)
    risk_items: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _title(text: str) -> str:
    text = _words(text)
    return text if len(text) <= _TITLE_CHARS else text[: _TITLE_CHARS - 3].rstrip() + "..."


def _named_items(line: Evidence) -> list[str]:
    seen: list[str] = []
    for found in _ITEM_ID.findall(f"{line.text} {line.quote or ''}"):
        if found not in seen:
            seen.append(found)
    return seen


def _reference(record: ChannelRecord, line: Evidence) -> dict:
    return {
        "ref": f"teams:{record.channel_id}:{line.message_id}", "channel_id": record.channel_id,
        "channel_display_name": record.channel_display_name, "date": record.date, "message_id": line.message_id,
        "section": line.section, "quote": line.quote,
    }


def _fingerprint(set_name: str, kind: str, record: ChannelRecord, line: Evidence, item_id: str | None) -> str:
    material = "|".join([set_name, kind, record.channel_id, line.message_id, item_id or "", _words(line.text)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _one_per_message(lines: list[Evidence]) -> list[Evidence]:
    """P1 splits a long message into several grounded lines. The reference is the message, so the lines of one message in one
    section are one thing here: their words joined, the first verbatim quote P1 gave."""
    groups: dict[tuple[str, str], list[Evidence]] = {}
    for line in lines:
        groups.setdefault((line.message_id, line.section), []).append(line)
    return [
        Evidence(message_id, " ".join(_words(m.text) for m in members), next((m.quote for m in members if m.quote), None), section)
        for (message_id, section), members in groups.items()
    ]


def plan_batches(record: ChannelRecord, view: TrackerView) -> Plan:
    """Both sets for one record. A pure function of the record and what the tracker and risk log say: the same inputs, the same plan."""
    plan, seen = Plan(), set()

    def add(items: list[dict], item: dict) -> None:
        if item["fingerprint"] not in seen:
            seen.add(item["fingerprint"])
            items.append(item)

    def skip(set_name: str, line: Evidence, reason: str) -> None:
        plan.skipped.append({"set": set_name, "message_id": line.message_id, "section": line.section, "reason": reason})

    for line in _one_per_message(record.evidence()):
        named = _named_items(line)
        known = [i for i in named if i in view.items]
        unknown = [i for i in named if i not in view.items]
        reference = _reference(record, line)
        blocker = line.section == "blocker"
        if unknown:
            reason = f"names {', '.join(unknown)}, which is not in the tracker"
            skip(TRACKER_SET, line, reason)
            if blocker and not known:
                skip(RISK_SET, line, reason)

        for item_id in known:
            add(plan.tracker_items, {
                "kind": "comment", "item_id": item_id, "tags": ["from-channel", line.section],
                "body": f"From {record.channel_display_name} ({record.date}): {_words(line.text)}",
                "source_text": line.text, "reference": reference, "fingerprint": _fingerprint(TRACKER_SET, "comment", record, line, item_id),
            })
            if not blocker:
                continue
            open_risk = view.open_risk_for(item_id)
            if open_risk is not None:
                skip(RISK_SET, line, f"{item_id} is already in the risk log as {open_risk.id}")
                continue
            add(plan.risk_items, {
                "kind": "new_risk", "related_item_id": item_id, "title": _title(line.text), "description": _words(line.text),
                "suggested_owner": view.items[item_id].assignee_id, "source_text": line.text, "reference": reference,
                "fingerprint": _fingerprint(RISK_SET, "new_risk", record, line, item_id),
            })

        if not named:
            if blocker:
                add(plan.tracker_items, {
                    "kind": "create", "title": _title(line.text), "status": "blocked", "assignee_id": None,
                    "sprint_id": view.sprint_covering(record.date), "source_text": line.text, "reference": reference,
                    "fingerprint": _fingerprint(TRACKER_SET, "create", record, line, None),
                })
                add(plan.risk_items, {
                    "kind": "new_risk", "related_item_id": None, "title": _title(line.text), "description": _words(line.text),
                    "suggested_owner": None, "source_text": line.text, "reference": reference,
                    "fingerprint": _fingerprint(RISK_SET, "new_risk", record, line, None),
                })
            else:
                skip(TRACKER_SET, line, "names no tracker item")
    return plan


# --- proposing -----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Batch:
    set_name: str
    proposal_id: str | None  # None: nothing to propose (nothing new, or everything was already decided against)
    created: bool
    items: int  # items in the proposal this describes (or, for a dry run, in the plan)
    remembered: int = 0  # items left out because an earlier proposal (pending, approved, applied or rejected) already has them


@dataclass(frozen=True)
class Consumed:
    record: ChannelRecord | None
    refused: RecordRefused | None
    tracker: Batch | None
    risk: Batch | None
    skipped: list[dict]
    plan: Plan | None = None


def _earlier_items(store: ProposalStore, proposal_type: str, channel_id: str) -> list[tuple[dict, str, str]]:
    """(item, proposal id, proposal status) for every item in every earlier batch of this type for this channel, oldest first."""
    found = []
    for status in (PENDING, APPROVED, REJECTED, APPLIED):
        for proposal in store.list_by_status(status):
            if proposal.type == proposal_type and proposal.payload.get("channel_id") == channel_id:
                found += [(item, proposal.id, proposal.status, getattr(proposal, "created_at", "") or "") for item in proposal.payload["items"]]
    found.sort(key=lambda row: row[3])
    return [(item, pid, status) for item, pid, status, _ in found]


def _key(proposal_type: str, record: ChannelRecord, fingerprints: list[str]) -> str:
    digest = hashlib.sha256("|".join(sorted(fingerprints)).encode("utf-8")).hexdigest()[:12]
    return f"{proposal_type}:{record.channel_id}:{record.date}:{digest}"


def _propose(set_name: str, proposal_type: str, items: list[dict], record: ChannelRecord, store: ProposalStore, db_path, dry_run: bool) -> Batch:
    if not items:
        return Batch(set_name, None, False, 0, 0)
    if dry_run:
        return Batch(set_name, None, False, len(items), 0)

    existing = store.get_by_idempotency_key(_key(proposal_type, record, [i["fingerprint"] for i in items]))
    if existing is not None and existing.status != REJECTED:
        return Batch(set_name, existing.id, False, len(items), len(items))

    earlier = _earlier_items(store, proposal_type, record.channel_id)
    known = {item["fingerprint"] for item, _, _ in earlier}
    fresh = [item for item in items if item["fingerprint"] not in known]
    if not fresh:
        return Batch(set_name, None, False, 0, len(items))

    for item in fresh:  # a line worded differently from one already proposed says what it replaces
        before = [(e, pid, status) for e, pid, status in earlier
                  if e["reference"]["message_id"] == item["reference"]["message_id"] and e["kind"] == item["kind"]
                  and e.get("item_id") == item.get("item_id") and e.get("related_item_id") == item.get("related_item_id")]
        if before:
            e, pid, status = before[-1]
            item["changed_since"] = {"earlier_proposal_id": pid, "earlier_status": status, "earlier_text": e["source_text"]}

    payload = {
        "channel_id": record.channel_id, "channel_display_name": record.channel_display_name, "date": record.date,
        "record_schema_version": record.schema_version, "record_generated_at": record.generated_at, "items": fresh,
    }
    refs = sorted({item["reference"]["ref"] for item in fresh})
    proposal = store.create(
        type=proposal_type, payload=payload, original_model_output={**payload, "model": None},
        source_refs=refs, idempotency_key=_key(proposal_type, record, [i["fingerprint"] for i in fresh]),
    )
    write_audit(db_path, actor=AGENT, action="proposal.created", proposal_id=proposal.id,
                details={"type": proposal_type, "channel_id": record.channel_id, "date": record.date, "items": len(fresh), "left_out_as_already_proposed": len(items) - len(fresh)})
    return Batch(set_name, proposal.id, True, len(fresh), len(items) - len(fresh))


def consume(path: str | Path, view: TrackerView, *, db_path: str | Path = DEFAULT_DB_PATH, dry_run: bool = False) -> Consumed:
    """Read one record file and propose its two batches. A refused record gives zero proposals and one audit row (not in a dry run)."""
    path = Path(path)
    try:
        record = load_record(path)
    except RecordRefused as refusal:
        if not dry_run:
            log_refusal(db_path, path, refusal, consumer=BATCHES)
        return Consumed(None, refusal, None, None, [])
    plan = plan_batches(record, view)
    store = ProposalStore(db_path)
    return Consumed(
        record, None,
        _propose(TRACKER_SET, CHANNEL_TRACKER_PROPOSAL_TYPE, plan.tracker_items, record, store, db_path, dry_run),
        _propose(RISK_SET, CHANNEL_RISK_PROPOSAL_TYPE, plan.risk_items, record, store, db_path, dry_run),
        plan.skipped, plan,
    )
