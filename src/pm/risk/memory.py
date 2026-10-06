"""PM-17: rejection memory. A rejected proposal is fingerprinted, so the next run does not
propose it again identically; a material change to the blocker allows a new proposal, with
the change stated. All of it is code: no model is asked, and none can alter what is stated.

The fingerprint is a hash of a blocker's MATERIAL facts: when it became blocked, who is
assigned, its title, its sprint, and the commitments made on it with their due dates.
The passing of time (the day count), new commits or chat on it, and the model's wording
are deliberately not material, so none of them can bring a rejected proposal back.

The memory is the proposal store itself: a rejected proposal stays in it, terminal, with
the fingerprint it was made from. `recall` compares the blocker as it is now with every
earlier proposal for that item and says what to do:

  new                      nothing earlier: propose
  already_proposed         an earlier proposal for exactly these facts is still open
  rejected_unchanged       exactly these facts were rejected before: do not propose again
  awaiting_decision        an earlier proposal (other facts) is still open: do not pile a second on it
  changed_since_rejection  every earlier one was rejected and the facts differ: propose, stating the change

Proposals made before fingerprints existed (PM-16) carry only the blocked-since date and
the owner; they are compared on exactly those, so nothing rejected is forgotten.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from spine.approval.proposals import REJECTED, Proposal

from pm.risk.gaps import BlockerGap

NEW = "new"
ALREADY_PROPOSED = "already_proposed"
REJECTED_UNCHANGED = "rejected_unchanged"
AWAITING_DECISION = "awaiting_decision"
CHANGED_SINCE_REJECTION = "changed_since_rejection"

_SCALARS = ("title", "assignee_id", "sprint_id", "blocked_since")


def material_facts(gap: BlockerGap) -> dict:
    return {
        "item_id": gap.item_id,
        "title": gap.title,
        "assignee_id": gap.assignee_id,
        "sprint_id": gap.sprint_id,
        "blocked_since": gap.blocked_since,
        "commitments": sorted(
            ({"id": c.id, "due_date_iso": c.due_date_iso, "text": c.text} for c in gap.commitments), key=lambda c: c["id"]
        ),
    }


def fingerprint(gap: BlockerGap) -> str:
    canonical = json.dumps(material_facts(gap), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def recorded_facts(proposal: Proposal) -> dict:
    """The material facts a proposal was made from. Older proposals recorded only the
    blocked-since date and the owner, and are compared on those alone."""
    payload = proposal.payload
    if "material_facts" in payload:
        return payload["material_facts"]
    owner = payload.get("suggested_owner")
    return {"blocked_since": (payload.get("facts") or {}).get("blocked_since"), "assignee_id": owner["id"] if owner else None}


@dataclass(frozen=True)
class Memory:
    state: str
    proposal: Proposal | None = None  # the one matched, blocking, or followed
    differences: list[dict] = field(default_factory=list)


def _differences(previous: dict, now: dict, names: dict[str, str]) -> list[dict]:
    """What differs between the facts a rejected proposal was made from and the facts now,
    over the facts the old record has."""

    def who(person_id):
        return names.get(person_id, person_id) if person_id else "nobody"

    out = []
    for key in _SCALARS:
        if key not in previous or previous[key] == now[key]:
            continue
        was, now_value = previous[key], now[key]
        text = {
            "title": f'title was "{was}", now "{now_value}"',
            "assignee_id": f"assignee was {who(was)}, now {who(now_value)}",
            "sprint_id": f"sprint was {was}, now {now_value}",
            "blocked_since": f"blocked since was {was or 'unknown'}, now {now_value or 'unknown'}",
        }[key]
        out.append({"field": key, "was": was, "now": now_value, "text": text})
    if "commitments" in previous:
        was_by_id = {c["id"]: c for c in previous["commitments"]}
        now_by_id = {c["id"]: c for c in now["commitments"]}
        for cid in sorted(set(was_by_id) | set(now_by_id)):
            before, after = was_by_id.get(cid), now_by_id.get(cid)
            if before == after:
                continue
            if before is None:
                text = f'new commitment {cid}: "{after["text"]}", due {after["due_date_iso"] or "no date"}'
            elif after is None:
                text = f"commitment {cid} is no longer recorded"
            elif before["due_date_iso"] != after["due_date_iso"]:
                text = f"commitment {cid} due date was {before['due_date_iso'] or 'none'}, now {after['due_date_iso'] or 'none'}"
            else:
                text = f'commitment {cid} was reworded: was "{before["text"]}", now "{after["text"]}"'
            out.append({"field": f"commitment:{cid}", "was": before, "now": after, "text": text})
    return out


def _same(proposal: Proposal, gap: BlockerGap, now: dict) -> bool:
    if "fingerprint" in proposal.payload:
        return proposal.payload["fingerprint"] == fingerprint(gap)
    return not _differences(recorded_facts(proposal), now, {})  # made before fingerprints existed


def recall(gap: BlockerGap, earlier: list[Proposal]) -> Memory:
    """`earlier`: every proposal ever made for this item, in any status."""
    if not earlier:
        return Memory(NEW)
    now = material_facts(gap)

    for proposal in sorted(earlier, key=lambda p: p.created_at, reverse=True):
        if _same(proposal, gap, now):
            return Memory(REJECTED_UNCHANGED if proposal.status == REJECTED else ALREADY_PROPOSED, proposal)

    open_ones = [p for p in earlier if p.status != REJECTED]
    if open_ones:
        return Memory(AWAITING_DECISION, max(open_ones, key=lambda p: p.created_at))

    latest = max(earlier, key=lambda p: p.decided_at or p.created_at)
    names = {gap.assignee_id: gap.assignee_name} if gap.assignee_id and gap.assignee_name else {}
    owner = latest.payload.get("suggested_owner")
    if owner and owner.get("id") not in names:
        names[owner["id"]] = owner["name"]
    differences = _differences(recorded_facts(latest), now, names)
    return Memory(CHANGED_SINCE_REJECTION, latest, differences) if differences else Memory(NEW)


def change_statement(memory: Memory, *, rejection_reason: str | None) -> dict:
    """The statement of change stored on (and printed in) a proposal that follows a rejection."""
    previous = memory.proposal
    decided = (previous.decided_at or "")[:10]
    text = (
        f"Changed since the rejected proposal {previous.id[:8]}"
        f"{f' ({decided})' if decided else ''}: " + "; ".join(d["text"] for d in memory.differences) + "."
    )
    who = f"Rejected by {previous.approver_id}" if previous.approver_id else "Rejected"
    text += f" {who}{f': {rejection_reason}' if rejection_reason else ''}."
    return {
        "previous_proposal_id": previous.id,
        "previous_status": REJECTED,
        "rejected_by": previous.approver_id,
        "rejected_at": previous.decided_at,
        "rejection_reason": rejection_reason,
        "differences": memory.differences,
        "text": text,
    }


__all__ = [
    "ALREADY_PROPOSED", "AWAITING_DECISION", "CHANGED_SINCE_REJECTION", "NEW", "REJECTED_UNCHANGED",
    "Memory", "change_statement", "fingerprint", "material_facts", "recall", "recorded_facts",
]
