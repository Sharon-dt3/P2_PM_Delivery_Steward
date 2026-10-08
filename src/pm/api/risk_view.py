"""The risk log in plain language, for a person asking about it in Teams.

Copilot Studio asks and the API answers with the rows AND a sentence or two a person can read on a phone. The sentences are built
here from the rows, by fixed templates, no model: so what the agent says about a risk is exactly what the log says, and nothing
it was not given. Reading changes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _ordered(risks):
    return sorted(risks, key=lambda r: (_SEVERITY_ORDER.get(r.severity, 3), r.id))


def risk_summary(risks, *, filters: Mapping[str, str | None] | None = None) -> str:
    """One line of counts, then one line per risk, most severe first."""
    asked = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in (filters or {}).items() if v)
    if not risks:
        return f"There are no risks in the log{f' for {asked}' if asked else ''}."
    by_status = {}
    for r in risks:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    counts = ", ".join(f"{n} {status}" for status, n in sorted(by_status.items()))
    head = f"{len(risks)} risk{'s' if len(risks) != 1 else ''}{f' for {asked}' if asked else ''} ({counts})."
    lines = [
        f"{r.id} ({r.severity}, {r.status})" + (f" on {r.related_item_id}" if r.related_item_id else "") + f": {r.title}"
        for r in _ordered(risks)
    ]
    return "\n".join([head, *lines])


def explain_risk(risk, item, owner_name: str | None, pending_titles: list[str]) -> str:
    """What the log says about one risk, the tracker item it is on, who has that item, and any proposal still waiting about it."""
    parts = [f"{risk.id} is a {risk.severity} risk, {risk.status}: {risk.title}.", risk.description.rstrip(".") + "."]
    if item is not None:
        owner = f", assigned to {owner_name}" if owner_name else ", with nobody assigned"
        blocked = f" and has been blocked since {item.blocked_since}" if item.blocked_since else ""
        parts.append(f"It is on {item.id} ({item.title}), which is {item.status}{owner}{blocked}.")
    elif risk.related_item_id:
        parts.append(f"It names {risk.related_item_id}, which is not in the tracker.")
    else:
        parts.append("It is not tied to a tracker item.")
    parts.append(f"Opened {risk.opened_at}.")
    if pending_titles:
        parts.append("Waiting for a decision: " + "; ".join(pending_titles) + ".")
    return " ".join(parts)
