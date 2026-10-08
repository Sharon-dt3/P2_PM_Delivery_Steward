"""Applying an approved risk-log proposal: what gets written to the risk log, decided in code.

A risk proposal comes in two shapes, and approving either one writes real rows to the risk log (the CSV system of record):

  risk_log_entry       one entry for a current blocker that has none (PM-16 / PM-19)
  channel_risk_entries a batch of entries from P1's channel outcome record (PM-26), each carrying the message that justifies it

Nothing here asks a model anything and nothing here is a surface: the service calls it, so the Teams card, the command line, the
dashboard and the API all write the same rows.

What is decided, and by whom:

  id          the next RISK-nnn after the highest in the log (code)
  title       the tracker item's own title for a gap; the batch item's title for a batch (never invented)
  description the proposal's own description, impact and, when promoted, drafted mitigation; for a batch, the line from the channel plus
              the message it came from
  opened_at   the date the evidence is as of (the proposal's own date), not the day someone pressed Approve
  owner       the owner the proposal SUGGESTED, written only when it was evidenced (the tracker's assignee of the item), as "Name (id)"; none
              when the proposal had none. Never guessed, never from the model; the evidence goes in the audit record
  severity    NOT the agent's: the proposals carry none (the brief asks for description, impact and suggested owner). The approver chooses it
              when approving. If they do not, `medium` is written and the audit says it was a default, so a lead can see which entries
              nobody rated and can change them in the CSV
  duplicates  a blocker that already has an OPEN risk is never written twice: refused for a single entry, skipped (and recorded) in a batch

A proposal that cannot be written as it stands is refused BEFORE it is approved, so nothing is left approved-but-not-applied.

Two copies of the log exist: the CSV (system of record) and the runtime copy in the database, which detection and the batch planner
read. Writing the CSV is the gated, fallible step; the runtime copy is then replaced from the CSV the way the sync does it (replace,
never append), so the same blocker is not proposed again tomorrow. If that refresh fails the write still stands and the audit says the
runtime copy is stale: the next sync or seed brings it level.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from spine.approval.proposals import Proposal

from pm.adapters.risk_log import Risk, RiskLogStore
from pm.adapters.tracker import ItemNotFoundError
from pm.channel.batches import CHANNEL_RISK_PROPOSAL_TYPE
from pm.risk.proposals import RISK_PROPOSAL_TYPE
from pm.risklog.csv_store import SEVERITIES, validate_risks

logger = logging.getLogger("pm.approval.risk_apply")
RISK_WRITE_TYPES = frozenset({RISK_PROPOSAL_TYPE, CHANNEL_RISK_PROPOSAL_TYPE})
DEFAULT_SEVERITY = "medium"
APPROVER_CHOSE, DEFAULTED = "approver", "default"

_NUMBER = re.compile(r"^RISK-(\d+)$")


class RiskApplyRefused(ValueError):
    """The proposal cannot be written to the risk log as it stands; the message says why in words."""


@dataclass(frozen=True)
class RiskWrite:
    entries: tuple[Risk, ...]
    skipped: tuple[dict, ...]  # items not written, each with the message it came from and why
    severity: str
    severity_source: str  # approver | default
    owners: tuple[dict, ...] = ()  # for each entry that got an owner: the risk id, the owner and the evidence it rests on


def choose_severity(requested: str | None) -> tuple[str, str]:
    """(severity, where it came from). A blank or missing choice is the default; anything else must be one of the log's own severities."""
    if requested is None or not str(requested).strip():
        return DEFAULT_SEVERITY, DEFAULTED
    chosen = str(requested).strip().lower()
    if chosen not in SEVERITIES:
        raise RiskApplyRefused(f"severity {requested!r} must be one of {', '.join(SEVERITIES)}")
    return chosen, APPROVER_CHOSE


def _next_numbers(existing: list[Risk]) -> int:
    return max((int(m.group(1)) for r in existing if (m := _NUMBER.match(r.id))), default=0) + 1


def _open_risk_for(existing: list[Risk], item_id: str | None) -> Risk | None:
    return next((r for r in existing if item_id and r.related_item_id == item_id and r.status == "open"), None)


def owner_label(person_id: str | None, name: str | None = None) -> str | None:
    """"Olivia Dupree (olivia.dupree)": the name and the id together, because two people can share a first name. Just the id when no name is known."""
    if not person_id:
        return None
    return f"{name} ({person_id})" if name and name != person_id else person_id


def _gap_entry(payload: dict, tracker, number: int, severity: str) -> Risk:
    item_id = payload["item_id"]
    try:
        title = f"{tracker.get_item(item_id).title} is blocked"
    except ItemNotFoundError:
        title = f"{item_id} is blocked"
    parts = [payload.get("description"), payload.get("impact")]
    if payload.get("mitigation"):
        parts.append(f"Mitigation: {payload['mitigation']}")
    suggested = payload.get("suggested_owner") or {}
    return Risk(
        id=f"RISK-{number:03d}", title=title, description=" ".join(p.strip() for p in parts if p and p.strip()),
        severity=severity, status="open", related_item_id=item_id, opened_at=payload["local_date"],
        owner=owner_label(suggested.get("id"), suggested.get("name")),
    )


def _batch_entry(item: dict, number: int, severity: str, fallback_date: str, names: dict[str, str]) -> Risk:
    ref = item["reference"]
    source = f"From {ref['channel_display_name']} message {ref['message_id']} on {ref['date']}."
    suggested = item.get("suggested_owner")  # the tracker's assignee of the item, as the batch proposed it
    return Risk(
        id=f"RISK-{number:03d}", title=item["title"], description=f"{item['description']} ({source})", severity=severity,
        status="open", related_item_id=item.get("related_item_id"), opened_at=ref.get("date") or fallback_date,
        owner=owner_label(suggested, names.get(suggested)),
    )


def plan_writes(proposal: Proposal, *, risk_log: RiskLogStore, tracker) -> RiskWrite:
    """What approving this proposal writes, without writing it. Raises RiskApplyRefused when nothing can be written.
    The severity is the one stored on the proposal when it was approved (`approved_severity`), or the default."""
    payload = proposal.payload
    severity, source = choose_severity(payload.get("approved_severity"))
    if payload.get("severity_source") in (APPROVER_CHOSE, DEFAULTED):
        source = payload["severity_source"]
    existing = risk_log.list_risks()
    number = _next_numbers(existing)
    entries: list[Risk] = []
    skipped: list[dict] = []

    if proposal.type == RISK_PROPOSAL_TYPE:
        covering = _open_risk_for(existing, payload.get("item_id"))
        if covering is not None:
            raise RiskApplyRefused(f"{payload['item_id']} is already in the risk log as {covering.id} (open), so a second entry is not written")
        entries.append(_gap_entry(payload, tracker, number, severity))
    elif proposal.type == CHANNEL_RISK_PROPOSAL_TYPE:
        names = {a.id: a.display_name for a in tracker.list_assignees()}
        covered = {r.related_item_id for r in existing if r.status == "open" and r.related_item_id}
        for item in payload.get("items", []):
            ref = item["reference"]
            where = {"channel": ref["channel_display_name"], "message_id": ref["message_id"], "title": item["title"]}
            related = item.get("related_item_id")
            if related and related in covered:
                skipped.append({**where, "reason": f"{related} already has an open risk"})
                continue
            if related:
                covered.add(related)  # two lines about one item in one batch make one entry
            entries.append(_batch_entry(item, number + len(entries), severity, payload.get("date", ""), names))
    else:
        raise RiskApplyRefused(f"{proposal.type} is not a risk-log proposal")

    if not entries:
        raise RiskApplyRefused("nothing to write: every item in this proposal is already covered by an open risk")
    problems = validate_risks([*existing, *entries])
    if problems:
        raise RiskApplyRefused("the risk log would be invalid: " + "; ".join(problems))
    owners = tuple(
        {"risk_id": e.id, "owner": e.owner, "evidence": _owner_evidence(proposal, e)} for e in entries if e.owner
    )
    return RiskWrite(tuple(entries), tuple(skipped), severity, source, owners)


def _owner_evidence(proposal: Proposal, entry: Risk) -> str:
    """Why this owner: what the proposal said it rested on (a gap says so itself; a batch takes the item's assignee in the tracker)."""
    if proposal.type == RISK_PROPOSAL_TYPE:
        return (proposal.payload.get("suggested_owner") or {}).get("evidence", "")
    return f"assignee of {entry.related_item_id} in the tracker"


def refresh_runtime_copy(risk_log: RiskLogStore, db_path) -> str:
    """Make the database's copy of the risk log the log just written. Returns "refreshed", or "stale: why" (never raises)."""
    from pm.risklog.csv_store import CsvRiskLog
    from pm.risklog.sync import RiskLogSync

    try:
        RiskLogSync(CsvRiskLog(), None, db_path=db_path)._refresh_runtime(risk_log.list_risks())
    except Exception as exc:  # noqa: BLE001 - the write already happened; the copy is derived and the next sync or seed rebuilds it
        logger.warning("risk log runtime copy not refreshed: %s: %s", type(exc).__name__, exc)
        return f"stale: {type(exc).__name__}: {exc}"[:200]
    return "refreshed"


def sync_lead_store(db_path) -> str:
    """After the risk log was written, bring the lead-facing table level: the same sync the morning job runs (pm.risklog.hook), so
    only the repo moved and it is pushed. One word, never raises: `off` (the sync is not switched on), `skipped` (not the configured
    database), `pushed`, `in_sync`, or why it did not happen (`conflict`, `remote_unreachable`, `error`...). The write already
    stands in the CSV, the system of record; a lead's table that is not level yet is brought level by the next sync."""
    from pm.risklog.hook import pull_lead_edits_if_enabled

    try:
        return pull_lead_edits_if_enabled(db_path)
    except Exception as exc:  # noqa: BLE001 - the hook itself never raises; this is belt and braces for the write that already happened
        logger.warning("lead-facing risk log sync failed: %s: %s", type(exc).__name__, exc)
        return "error"


LEAD_LEVEL = frozenset({"pushed", "in_sync"})
