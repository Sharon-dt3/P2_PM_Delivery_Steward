"""A channel brief as the message that is posted: fixed templates over the facts (pm.channelbrief.facts). No model, no rewording.

Every line is one of P1's grounded lines, shown exactly as P1 verified it with the message it came from, or a fact computed in code from
real rows (a promise's due date, an item this agent made from a message, who had no say). The same facts always make the same message.

A section with nothing in it says so ("none recorded") instead of vanishing: a quiet channel gets an honest quiet brief, not a padded one.
Long sections are cut at LINES_PER_SECTION and say how many more there are; nothing is dropped silently.
"""

from __future__ import annotations

import os

from pm.approval.proposals import format_brief_message, format_summary_message
from pm.channelbrief.facts import MORNING, ChannelBriefFacts, Line

LINES_PER_SECTION = 5
ENV_LINES = "PM_CHANNEL_BRIEF_LINES"
TITLES = {"blockers": "Blockers", "decisions": "Decisions", "updates": "Updates", "questions": "Open questions"}
PROMISE_STATE = {
    "past_due": "past its due date; nothing here says it was done",
    "due_today": "due today",
    "upcoming": "coming up",
    "no_date": "no date given",
}


def _limit() -> int:
    raw = (os.environ.get(ENV_LINES) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else LINES_PER_SECTION


def _who(line: Line) -> str:
    return f"{line.author}, " if line.author else ""


def _source(line: Line) -> str:
    """How a reader gets to the message: P1's own link to it when P1 has one (as in P1's daily digest), else the message id."""
    return f"[source]({line.url})" if line.url else f"message {line.message_id}"


def _line(line: Line) -> str:
    return f"- {line.text} ({_who(line)}{_source(line)})"


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


def render_content(facts: ChannelBriefFacts) -> str:
    """The body of the message (without its dated title)."""
    limit = _limit()
    out = [f"{facts.channel_name}: what the team said, from P1's record for {facts.record_date}."]
    if facts.kind == MORNING and facts.record_age_days > 1:
        out.append(f"This is the newest record on file, {_days(facts.record_age_days)} old: nothing newer has been recorded.")
    out.append("Every line below is quoted from that record or computed from it; none of it is generated.")

    for name in ("blockers", "decisions", "updates", "questions"):
        lines = facts.sections[name]
        out += ["", f"## {TITLES[name]} ({len(lines)})"]
        if not lines:
            out.append("- none recorded")
            continue
        out += [_line(line) for line in lines[:limit]]
        if len(lines) > limit:
            out.append(f"- and {len(lines) - limit} more in the record ({len(lines)} in all)")

    if facts.promises:
        out += ["", f"## Said they would ({len(facts.promises)})"]
        for p in facts.promises[:limit]:
            due = f"due {p.due_iso}" if p.due_iso else "no date given"
            out.append(f"- {_who(p.line)}\"{p.line.text}\" ({due}: {PROMISE_STATE[p.state]}; {_source(p.line)})")
        if len(facts.promises) > limit:
            out.append(f"- and {len(facts.promises) - limit} more")

    if facts.work:
        out += ["", f"## Turned into work ({len(facts.work)})"]
        for w in facts.work[:limit]:
            out.append(f"- {w.ref} ({w.label}): {w.title} (from message {w.message_id})")
        if len(facts.work) > limit:
            out.append(f"- and {len(facts.work) - limit} more")

    if facts.unassigned:
        out += ["", f"## Nobody owns these yet ({len(facts.unassigned)})"]
        for w in facts.unassigned[:limit]:
            out.append(f"- {w.ref} ({w.label}): {w.title} (from message {w.message_id})")
        if len(facts.unassigned) > limit:
            out.append(f"- and {len(facts.unassigned) - limit} more")

    if facts.silent:
        out += ["", "## No say that day"]
        out += [f"- {who}: {what.lower()}" for who, what in facts.silent]
    return "\n".join(out)


def render_message(facts: ChannelBriefFacts, *, label: str = "") -> str:
    """The exact text posted: an optional label, the dated title, then the brief."""
    content = render_content(facts)
    return (format_brief_message if facts.kind == MORNING else format_summary_message)(label, facts.local_date, content)


def evidence_lines(facts: ChannelBriefFacts) -> list[dict]:
    """Every line the message carries, with the message it rests on, in the shape a proposal keeps (what the agent proposed, untouched)."""
    lines = [{"section": line.section, "reference_id": line.message_id, "text": line.text, "quote": line.quote} for line in facts.lines()]
    lines += [{"section": "promise", "reference_id": p.line.message_id, "text": p.line.text, "quote": p.line.quote} for p in facts.promises]
    lines += [{"section": f"work:{w.kind}", "reference_id": w.message_id, "text": f"{w.ref}: {w.title}", "quote": None} for w in facts.work]
    lines += [{"section": "unassigned:tracker", "reference_id": w.message_id, "text": f"{w.ref}: {w.title}", "quote": None} for w in facts.unassigned]
    return lines
