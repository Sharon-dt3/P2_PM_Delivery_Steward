"""
Morning brief expression (PM-08), grounding (PM-09), and honest absence
(PM-10).

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

PM-10 ("A person with no activity is reported as having no activity --
not omitted, not embellished") is rendered here, in _render_brief, and
ONLY here -- no model call, no new prompt, no new section. This mirrors
P1's own participation ledger discipline exactly (see
../P3_Agents/src/p1/reporting/participation_rendering.py's own
docstring): an absence is not a fact that needs phrasing, so there is
nothing to hand the model. person.has_activity (pm.reporting.facts) is
the one, already-computed signal this checks; when it's False, the four
normal Committed/Delivered/Pending/Blocked lines (which would otherwise
each read "none.", an accurate but noisy way to say the same thing four
times) are replaced by exactly one fixed-wording line,
_NO_ACTIVITY_LINE -- a literal constant, never interpolated, so an
absence can never be embellished with an inferred reason the way P1's
own PARTICIPATION_WORDING never is either. Row-level acceptance test,
literal: "The zero-activity assignee appears with an explicit no-update
line." Proven in tests/unit/test_reporting_morning_brief.py against a
person with every bucket and commit_count empty (a person whose only
activity is commits gets one fixed "Commits: N recorded; no tracker items."
line instead -- see _commits_only_line), and in
tests/unit/test_reporting_facts.py and test_state_snapshot.py against
this repo's own real seeded roster, which PM-10 gives a seventh,
deliberately silent member for exactly this purpose (see
pm/seed/build.py's own comment on ASSIGNEES).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import BaseModel
from spine.grounding.kernel import (
    FactualLine,
    GroundingFailure,
    GroundingResult,
    ground_with_retry,
)
from spine.llm.structured import generate_structured
from spine.prompts.registry import PromptRegistry

from pm.reporting.facts import MorningBriefFacts

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

# PM-10's own fixed wording for a genuinely zero-activity person -- see
# this module's own docstring for why it is a literal constant, never
# built up from parts, exactly like P1's own PARTICIPATION_WORDING.
_NO_ACTIVITY_LINE = "No update: no tracker activity or commits recorded."



def _commits_only_line(commit_count: int) -> str:
    """PM-10: someone whose only activity is commits (no tracker items) is
    shown the commit count -- a fact computed in code -- rather than four
    "none."s that would hide it. A template, never model prose."""
    return f"Commits: {commit_count} recorded; no tracker items."


# Appended to a fact shown as the fact itself because its generated line was
# dropped for good: the reader sees the recorded fact, flagged as not phrased
# by the model, never a false "none.".
_AS_RECORDED = "[as recorded]"

ReferenceLookup = Callable[[str], str | None]

MIN_QUOTE_CHARS = 8
_ID_RE = re.compile(r"\b[A-Za-z]+-\d+\b")  # PM-009, RISK-001, sprint-13
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _specifics(text: str) -> tuple[dict[str, str], dict[float, str]]:
    """The checkable specifics in a piece of text: ids (compared
    case-insensitively) and numbers (compared by value, so the 7 in
    "7 September" matches the 07 in "2026-09-07"). Each maps to the form it
    was written in, for use in messages."""
    ids = {m.lower(): m for m in _ID_RE.findall(text)}
    numbers = {float(m): m for m in _NUMBER_RE.findall(_ID_RE.sub(" ", text))}
    return ids, numbers


_WORD_RE = re.compile(r"[A-Za-z]+")

# Plain connecting and status words a faithful line may use without the fact
# containing them. Deliberately short and conservative: anything that asserts
# something (an outcome, a manner, a person or party, a speed) is NOT here.
_ALLOWED_WORDS = frozenset(
    ["the", "and", "but", "or", "nor", "of", "to", "in", "on", "at", "for", "by", "with", "from", "as", "into", "onto", "over", "about", "is", "are", "was", "were", "be", "been", "being", "am", "has", "have", "had", "having", "do", "does", "did", "it", "its", "this", "that", "these", "those", "there", "here", "their", "his", "her", "they", "them", "he", "she", "who", "which", "then", "than", "also", "not", "no", "yes", "only", "just", "still", "yet", "already", "now", "today", "yesterday", "deliver", "delivered", "complete", "completed", "finish", "finished", "done", "ship", "shipped", "shipping", "commit", "committed", "pending", "open", "blocked", "block", "waiting", "item", "items", "sprint", "day", "days", "due", "date", "scope", "risk", "severity", "blocker", "blockers", "assignee", "run", "runs", "running", "start", "starts", "started", "end", "ends", "ended", "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december", "through", "until", "before", "after", "during", "within", "up", "out", "off", "all", "both", "each", "one", "two", "remains", "remain", "remaining", "left", "ready", "linked", "tied", "related", "relates"]
)


def _stem(word: str) -> str:
    """Crude suffix-stripping so inflections of a word the fact already uses
    (ship / shipping / shipped, page / pages) count as the same word."""
    w = word.lower()
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    return w


_ALLOWED_STEMS = frozenset(_stem(w) for w in _ALLOWED_WORDS)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(_ID_RE.sub(" ", text))


def _unsupported_words(text: str, source: str) -> list[str]:
    """Content words in `text` that neither appear in `source` (compared by
    stem) nor are plain connecting/status words. In order, deduplicated, in
    the form the line wrote them."""
    known = {_stem(w) for w in _words(source)} | _ALLOWED_STEMS
    found: dict[str, str] = {}
    for word in _words(text):
        if len(word) <= 2:
            continue
        if _stem(word) not in known:
            found.setdefault(word.lower(), word)
    return list(found.values())


def _check_line_content(line: FactualLine, source: str) -> str | None:
    """Layers two and three of the wording checks (layer one is the kernel's
    own verbatim-quote verification, which runs before this). Returns why a
    line that cites a real fact still is not supported by it, or None.

    - it must carry a quote of at least MIN_QUOTE_CHARS characters, so a
      line is anchored to specific words of its source (the kernel then
      verifies that quote is literally in the source);
    - every item/risk id and every number in its text must also appear in
      the source: the model may not introduce a name, count or date the fact
      does not contain;
    - every other content word must be one of the fact's own words (by stem)
      or a plain connecting/status word, so "...and got praise from the
      client" is caught even though it has no id or number.

    Still unable to catch: a rearrangement of the fact's own words that
    changes what they mean, or a synonym on the allowlist used wrongly."""
    if len((line.quote or "").strip()) < MIN_QUOTE_CHARS:
        return (
            f"line must include a `quote`: an exact fragment of at least {MIN_QUOTE_CHARS} "
            "characters copied from the fact it cites"
        )
    line_ids, line_numbers = _specifics(line.text)
    source_ids, source_numbers = _specifics(source)
    extra = [form for key, form in line_ids.items() if key not in source_ids]
    extra += [form for value, form in line_numbers.items() if value not in source_numbers]
    if extra:
        return f"line mentions {', '.join(extra)}, which does not appear in the fact it cites"
    words = _unsupported_words(line.text, source)
    if words:
        return (
            f"line uses words that are not in the fact it cites: {', '.join(words)}. "
            "Say only what the fact says, using its own words"
        )
    return None


class MorningBriefLineDraft(BaseModel):
    """The model's own one-line prose for exactly one input fact.
    reference_id is expected to be echoed back exactly as given, and quote
    to be an exact fragment of that fact's own detail text -- the grounding
    kernel is what actually enforces both, not this schema, since a schema
    can only check shape, not truth."""

    text: str
    reference_id: str | None = None
    quote: str | None = None


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
                    detail=f'{person.name} committed: "{commitment.text}" (due {due}).',
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
                    detail=f"{person.name}: {item.item_id} ({item.title}).",
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
                    f"(item {blocker.related_item_id or 'none'}, assignee {blocker.assignee_name or blocker.assignee_id or 'unassigned'})."
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
) -> GroundingResult:
    """Returns a GroundingResult for one section. Calls the model at most
    once per retry attempt, and never at all when items is empty -- an
    empty section is an honest fact (nothing pending, say), not a prompt
    to fill in.

    A line is grounded against THIS section's own facts only, never the
    whole brief's: a real item cited under the wrong section (a pending
    item claimed as "delivered") is as unsupported as an invented one, and
    a brief-wide lookup would let it through (found by PM-12's GC2 probe,
    the same cross-section leak P1's per-section message_lookup closes)."""
    if not items:
        return GroundingResult()

    section_lookup: ReferenceLookup = {item.reference_id: item.detail for item in items}.get

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
            FactualLine(text=line.text, message_id=line.reference_id, quote=line.quote) for line in draft.lines
        ]

    return ground_with_retry(generate_fn, section_lookup, content_check=_check_line_content)


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


def _identified(line: FactualLine, facts: MorningBriefFacts) -> str:
    """A blocker line says which risk it is and how severe, whatever the model's wording: if the model left the
    risk id or the severity tag out, they are put in front, once; a line that already carries them is untouched.
    (Found by golden case 9 on a real model, which wrote the sentence alone.)"""
    blocker = next((b for b in facts.blockers if _reference("risk", b.risk_id) == line.message_id), None)
    if blocker is None:
        return line.text
    severity = "" if f"[{blocker.severity}]" in line.text else f"[{blocker.severity}] "
    risk = "" if blocker.risk_id in line.text else f"{blocker.risk_id}: "
    return f"{severity}{risk}{line.text}"


def _render_brief(facts: MorningBriefFacts, sections: dict[str, list[FactualLine]]) -> str:
    """Deterministic assembly of the grounded lines into the final brief
    text -- no model call happens here. A fact whose line was dropped for
    good is shown as the recorded fact itself, marked _AS_RECORDED; "none."
    and "no sprint on file" appear only when there truly is nothing, never as
    a stand-in for a dropped line."""
    parts: list[str] = []
    section_facts = _section_facts(facts)

    def recorded(section_key: str, ref: str) -> str:
        detail = next(f.detail for f in section_facts[section_key] if f.reference_id == ref)
        return f"{detail} {_AS_RECORDED}"

    scope_lines = sections["sprint_scope"]
    if scope_lines:
        parts.append(scope_lines[0].text)
    elif section_facts["sprint_scope"]:
        parts.append(recorded("sprint_scope", section_facts["sprint_scope"][0].reference_id))
    else:
        parts.append("Sprint scope: no sprint on file covers this date.")
    parts.append("")

    # Keyed by (section, ref), never ref alone: one item can be both a
    # commitment and a delivered/pending/blocked item, and the two
    # sections' lines share that item's reference_id.
    texts_by_section_ref: dict[tuple[str, str], list[str]] = {}
    for key in ("committed", "delivered", "pending", "blocked"):
        for line in sections[key]:
            if line.message_id:
                texts_by_section_ref.setdefault((key, line.message_id), []).append(line.text)

    # A person's detail text names them ("aisha.rahman: PM-001 (...)"), so a
    # fallback is looked up by (section, ref, assignee) from the detail lines.
    details_by_section_ref: dict[tuple[str, str], list[str]] = {}
    for key in ("committed", "delivered", "pending", "blocked"):
        for fact in section_facts[key]:
            details_by_section_ref.setdefault((key, fact.reference_id), []).append(fact.detail)

    for person in facts.people:
        parts.append(f"## {person.name}")

        if not person.has_activity:
            # PM-10: a genuinely zero-activity person gets exactly one
            # explicit line, not four repeated "none."s -- see this
            # module's own docstring.
            parts.append(f"- {_NO_ACTIVITY_LINE}")
            parts.append("")
            continue

        if not (person.committed or person.delivered or person.pending or person.blocked):
            # has_activity is true, so the only activity is commits.
            parts.append(f"- {_commits_only_line(person.commit_count)}")
            parts.append("")
            continue

        # Ordered lists (deduplicated), not sets: set iteration order would
        # make the rendered text differ run to run for identical facts.
        committed_refs = dict.fromkeys(
            _reference("item", c.item_id) if c.item_id else _reference("commitment", str(c.id))
            for c in person.committed
        )
        delivered_refs = dict.fromkeys(_reference("item", i.item_id) for i in person.delivered)
        pending_refs = dict.fromkeys(_reference("item", i.item_id) for i in person.pending)
        blocked_refs = dict.fromkeys(_reference("item", i.item_id) for i in person.blocked)

        for label, section_key, refs in (
            ("Committed", "committed", committed_refs),
            ("Delivered", "delivered", delivered_refs),
            ("Pending", "pending", pending_refs),
            ("Blocked", "blocked", blocked_refs),
        ):
            lines: list[str] = []
            for ref in refs:
                grounded = texts_by_section_ref.get((section_key, ref))
                if grounded:
                    lines.extend(grounded)
                else:
                    owned = [
                        d for d in details_by_section_ref.get((section_key, ref), [])
                        if d.startswith(person.name)
                    ]
                    lines.extend(f"{d} {_AS_RECORDED}" for d in owned)
            parts.append(f"- {label}: " + " ".join(lines) if lines else f"- {label}: none.")
        parts.append("")

    if facts.unassigned:
        # Computed in code, never worded by a model: each line is the recorded fact, and cites the item it is about.
        parts.append("## Nobody is assigned")
        parts += [f"- {u.item_id} ({u.title}): nobody is assigned; status {u.status}." for u in facts.unassigned]
        parts.append("")

    parts.append("## Blockers")
    blocker_lines = sorted(sections["blockers"], key=_order_key("blockers", facts))
    grounded_refs = {line.message_id for line in blocker_lines}
    if blocker_lines or section_facts["blockers"]:
        for line in blocker_lines:
            parts.append(f"- {_identified(line, facts)}")
        for fact in section_facts["blockers"]:  # facts.blockers' own severity order
            if fact.reference_id not in grounded_refs:
                parts.append(f"- {fact.detail} {_AS_RECORDED}")
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
    section_facts = _section_facts(facts)

    sections: dict[str, list[FactualLine]] = {}
    dropped: dict[str, list[dict]] = {}
    for section_key in SECTION_ORDER:
        result = _generate_section_lines(
            gateway, prompt, section_key, section_facts[section_key]
        )
        sections[section_key] = result.grounded_lines
        dropped[section_key] = _failures_as_dicts(result.failures)

    content = _render_brief(facts, sections)
    return MorningBrief(facts=facts, sections=sections, dropped=dropped, content=content)
