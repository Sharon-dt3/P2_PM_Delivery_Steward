"""
Morning brief expression (PM-08) and grounding (PM-09).

Facts in code (pm.reporting.facts, zero import of spine.llm/spine.prompts
anywhere in it), prose from the model here -- the same split proven in
P1 (src/p1/reporting/daily_summary.py). The model is asked only to turn
MorningBriefFacts into readable prose, one short line per fact, never to
decide what counts as a fact in the first place.

PM-09's own job, wired in here rather than reinvented: every line the
model writes must carry a reference_id that traces back to something
real in the given facts -- an item id, a risk id, or a sprint id (this
brief's own three reference kinds; a future report that cites a commit
hash or a Teams message id directly would extend _build_reference_lookup()
the exact same way, not build a second mechanism). This module calls
spine.grounding.kernel.ground_with_retry() DIRECTLY -- the same function
P1's own daily/weekly summaries call -- rather than reimplementing
"reference-or-drop": PM-09's own WBS row calls this "wiring, not
building," and that is exactly what this module does. A line with no
reference_id, or one that doesn't resolve, is dropped and logged by the
kernel itself; it never reaches the rendered brief.

This module asks for one line PER FACT (one per delivered/pending/
blocked item, one per commitment, one per open risk, one for the sprint-
scope opener) -- never one big paragraph -- mirroring exactly how P1's
own daily_summary.py asks for "one line per input fact" (see that
module's own docstring): a whole free-text paragraph cannot be grounded
line by line, only a batch of individually-referenced lines can.
Assembling the surviving grounded lines into the final brief text is
deterministic Python (_render_brief), never the model's job -- the model
never decides structure or ordering, only phrases one already-true fact
at a time. This replaces this module's own earlier (PM-08) single-
narrative-plus-regex-check design, which could not individually drop one
bad line from an otherwise-good paragraph.

Row-level acceptance test, literal: "A hand-forged unreferenced line is
dropped and logged." Proven directly in
tests/unit/test_reporting_morning_brief.py: a FakeGateway response
carrying one line with no reference_id (hand-forged, not something any
real fact-rendering in this module would ever produce) is excluded from
the final brief's content, appears in the returned `dropped` mapping,
and is logged via the kernel's own `spine.grounding.kernel` logger
(captured with caplog).
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from pm.reporting.facts import MorningBriefFacts
from spine.grounding.kernel import FactualLine, GroundingFailure, GroundingResult, ground_with_retry
from spine.llm.structured import generate_structured
from spine.prompts.registry import PromptRegistry

MORNING_BRIEF_CAPABILITY = "pm08_morning_brief"

SECTION_ORDER = ("sprint_scope", "committed", "delivered", "pending", "blocked", "blockers")

_SECTION_LABELS = {
    "sprint_scope": "sprint scope",
    "committed": "what each person committed to",
    "delivered": "what each person delivered",
    "pending": "what each person still has pending",
    "blocked": "what each person is blocked on",
    "blockers": "blockers, ranked by impact",
}

ReferenceLookup = Callable[[str], str | None]


class MorningBriefLineDraft(BaseModel):
    """The model's own one-line prose for exactly one input fact.
    reference_id is expected to be echoed back exactly as given -- the
    grounding kernel is what actually enforces that it is, not this
    schema, since a schema can only check shape, not truth."""

    text: str
    reference_id: str | None = None


class MorningBriefSectionDraft(BaseModel):
    lines: list[MorningBriefLineDraft]


class MorningBrief(BaseModel):
    facts: MorningBriefFacts
    sections: dict[str, list[FactualLine]]  # grounded lines, keyed by SECTION_ORDER
    dropped: dict[str, list[dict]]  # GroundingFailure, dumped to plain dicts (see below)
    content: str


def _reference(kind: str, id_: str) -> str:
    return f"{kind}:{id_}"


def _build_reference_lookup(facts: MorningBriefFacts) -> ReferenceLookup:
    """Every reference_id a line in this brief could legitimately cite,
    mapped to a short human-readable label -- built once from `facts`
    alone, so a line can only ground against something this brief was
    actually given, never anything else on file."""
    table: dict[str, str] = {}
    for person in facts.people:
        for bucket in (person.delivered, person.pending, person.blocked):
            for item in bucket:
                table[_reference("item", item.item_id)] = item.title
        for commitment in person.committed:
            if commitment.item_id:
                table[_reference("item", commitment.item_id)] = commitment.text
            table[_reference("commitment", str(commitment.id))] = commitment.text
    for blocker in facts.blockers:
        table[_reference("risk", blocker.risk_id)] = blocker.title
        if blocker.related_item_id:
            table.setdefault(_reference("item", blocker.related_item_id), blocker.title)
    if facts.sprint is not None:
        table[_reference("sprint", facts.sprint.sprint_id)] = facts.sprint.display_name
    return table.get


class _FactLine(BaseModel):
    """One numbered input fact handed to the model for one section: a
    reference_id we already know is real (computed from `facts`, never
    from the model), plus a plain-text detail the model paraphrases into
    prose. The model's job is to echo reference_id back verbatim and
    phrase detail -- never to invent either."""

    reference_id: str
    detail: str


def _sprint_scope_facts(facts: MorningBriefFacts) -> list[_FactLine]:
    if facts.sprint is None:
        return []
    sprint = facts.sprint
    return [
        _FactLine(
            reference_id=_reference("sprint", sprint.sprint_id),
            detail=(
                f"{sprint.display_name} ({sprint.sprint_id}), day {sprint.day_number} of "
                f"{sprint.total_days} ({sprint.start_date} to {sprint.end_date}); "
                f"{sprint.done_items} of {sprint.total_items} items in this sprint are done."
            ),
        )
    ]


def _committed_facts(facts: MorningBriefFacts) -> list[_FactLine]:
    out = []
    for person in facts.people:
        for commitment in person.committed:
            ref = _reference("item", commitment.item_id) if commitment.item_id else _reference(
                "commitment", str(commitment.id)
            )
            due = commitment.due_date_iso or commitment.due_date_text or "no date given"
            out.append(
                _FactLine(
                    reference_id=ref,
                    detail=f'{person.assignee_id} committed: "{commitment.text}" (due {due}).',
                )
            )
    return out


def _item_bucket_facts(facts: MorningBriefFacts, bucket_name: str) -> list[_FactLine]:
    out = []
    for person in facts.people:
        for item in getattr(person, bucket_name):
            out.append(
                _FactLine(
                    reference_id=_reference("item", item.item_id),
                    detail=f"{person.assignee_id}: {item.item_id} ({item.title}).",
                )
            )
    return out


def _blocker_facts(facts: MorningBriefFacts) -> list[_FactLine]:
    out = []
    for blocker in facts.blockers:
        out.append(
            _FactLine(
                reference_id=_reference("risk", blocker.risk_id),
                detail=(
                    f"[{blocker.severity}] {blocker.risk_id}: {blocker.title} "
                    f"(item {blocker.related_item_id or 'none'}, assignee {blocker.assignee_id or 'unassigned'})."
                ),
            )
        )
    return out


def _section_facts(facts: MorningBriefFacts) -> dict[str, list[_FactLine]]:
    return {
        "sprint_scope": _sprint_scope_facts(facts),
        "committed": _committed_facts(facts),
        "delivered": _item_bucket_facts(facts, "delivered"),
        "pending": _item_bucket_facts(facts, "pending"),
        "blocked": _item_bucket_facts(facts, "blocked"),
        "blockers": _blocker_facts(facts),
    }


def _render_facts_block(items: list[_FactLine]) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. reference_id: {item.reference_id}\n   detail: {item.detail}")
    return "\n".join(lines)


def _generate_section_lines(
    gateway,
    prompt,
    section_key: str,
    items: list[_FactLine],
    reference_lookup: ReferenceLookup,
) -> GroundingResult:
    """Returns a GroundingResult for one section. Calls the model at most
    once per retry attempt, and never at all when items is empty -- an
    empty section is an honest fact (nothing pending, say), not a prompt
    to fill in."""
    if not items:
        return GroundingResult()

    facts_block = _render_facts_block(items)

    def generate_fn(feedback: str | None) -> list[FactualLine]:
        feedback_block = f"\n{feedback}\n" if feedback else ""
        rendered = prompt.render(
            section_label=_SECTION_LABELS[section_key],
            facts_block=facts_block,
            feedback_block=feedback_block,
        )
        draft = generate_structured(
            gateway, rendered, MorningBriefSectionDraft, tool_name="morning_brief_section"
        )
        return [
            FactualLine(text=line.text, message_id=line.reference_id, quote=None) for line in draft.lines
        ]

    return ground_with_retry(generate_fn, reference_lookup)


def _order_key(section_key: str, facts: MorningBriefFacts) -> Callable[[FactualLine], int]:
    """Grounded lines come back in whatever order the model returned
    them; blockers in particular must stay in facts.blockers' own
    severity-ranked order regardless (never the model's own ordering).
    Every other section's order doesn't matter for rendering (each line
    is placed by its own owning person), so this only does real work for
    "blockers"."""
    if section_key != "blockers":
        return lambda line: 0
    position = {_reference("risk", blocker.risk_id): index for index, blocker in enumerate(facts.blockers)}
    return lambda line: position.get(line.message_id, len(position))


def _render_brief(facts: MorningBriefFacts, sections: dict[str, list[FactualLine]]) -> str:
    """Deterministic assembly of the grounded lines into the final brief
    text -- no model call happens here. A bucket that ends up empty
    (because it had no facts, or every line in it was dropped) is
    rendered as an honest, explicit statement, never silently omitted."""
    parts: list[str] = []

    scope_lines = sections["sprint_scope"]
    parts.append(scope_lines[0].text if scope_lines else "Sprint scope: no sprint on file covers this date.")
    parts.append("")

    text_by_item_ref: dict[str, str] = {
        line.message_id: line.text
        for key in ("committed", "delivered", "pending", "blocked")
        for line in sections[key]
        if line.message_id
    }

    for person in facts.people:
        parts.append(f"## {person.assignee_id}")

        committed_refs = {
            _reference("item", c.item_id) if c.item_id else _reference("commitment", str(c.id))
            for c in person.committed
        }
        delivered_refs = {_reference("item", i.item_id) for i in person.delivered}
        pending_refs = {_reference("item", i.item_id) for i in person.pending}
        blocked_refs = {_reference("item", i.item_id) for i in person.blocked}

        for label, refs in (
            ("Committed", committed_refs),
            ("Delivered", delivered_refs),
            ("Pending", pending_refs),
            ("Blocked", blocked_refs),
        ):
            lines = [text_by_item_ref[ref] for ref in refs if ref in text_by_item_ref]
            if lines:
                parts.append(f"- {label}: " + " ".join(lines))
            else:
                parts.append(f"- {label}: none.")
        parts.append("")

    parts.append("## Blockers")
    blocker_lines = sorted(sections["blockers"], key=_order_key("blockers", facts))
    if blocker_lines:
        for line in blocker_lines:
            parts.append(f"- {line.text}")
    else:
        parts.append("- none.")

    return "\n".join(parts)


def _failures_as_dicts(failures: list[GroundingFailure]) -> list[dict]:
    return [{"reason": f.reason, "detail": f.detail, "text": f.line.text} for f in failures]


def generate_morning_brief(
    facts: MorningBriefFacts,
    gateway,
    *,
    prompt_registry: PromptRegistry | None = None,
) -> MorningBrief:
    """Turns `facts` into a grounded MorningBrief via `gateway`. `facts`
    is never recomputed here -- this function's whole job is expression
    and grounding, not fact-gathering (see pm.reporting.facts for that
    half of the split)."""
    registry = prompt_registry or PromptRegistry()
    prompt = registry.get(MORNING_BRIEF_CAPABILITY)
    reference_lookup = _build_reference_lookup(facts)
    section_facts = _section_facts(facts)

    sections: dict[str, list[FactualLine]] = {}
    dropped: dict[str, list[dict]] = {}
    for section_key in SECTION_ORDER:
        result = _generate_section_lines(
            gateway, prompt, section_key, section_facts[section_key], reference_lookup
        )
        sections[section_key] = result.grounded_lines
        dropped[section_key] = _failures_as_dicts(result.failures)

    content = _render_brief(facts, sections)
    return MorningBrief(facts=facts, sections=sections, dropped=dropped, content=content)
