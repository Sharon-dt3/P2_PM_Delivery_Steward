"""The weekly report's narrative (PM-29): the one part a model writes, held to the facts.

Python computes every quantity (pm.reporting.weekly). The model is asked only for plain-language prose about facts it is handed, in two places:

  lines    "what changed alongside this week's velocity": one plain line per fact (the velocity change, each item added after planning, each blocked
           item, each unmapped status). Each line cites its fact's reference_id and quotes it verbatim; it is grounded exactly like the morning brief's
           lines (spine's reference-or-drop kernel plus the wording check): a line that cites nothing real, quotes nothing real, or brings in an id, number or
           word its fact does not have is dropped, logged, and shown as the recorded fact instead.
  closing  one sentence in ordinary words on how the week went against the one before. P1's weekly roll-up rule applies: it may state no figure, and it
           is enforced, not requested: a digit, a number word, an id, a claim of cause, or a word that is in none of the facts is refused and the model is
           asked again with the reason; if it never complies the report simply has no closing sentence.

A fact's wording is the same text the report's own sections state, so a model line can only restate what the report already says, in plainer words.
With no facts to rephrase the model is not called. The model never sees or decides a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel
from spine.grounding.kernel import FactualLine, GroundingResult, ground_with_retry
from spine.llm.structured import generate_structured
from spine.prompts.registry import PromptRegistry

from pm.reporting.morning_brief import (
    _AS_RECORDED,
    MorningBriefSectionDraft,
    _check_line_content,
    _unsupported_words,
)
from pm.reporting.weekly import WeeklyFacts

NARRATIVE_CAPABILITY = "pm29_weekly_narrative"
CLOSING_CAPABILITY = "pm29_weekly_closing"
SECTION_LABEL = "What changed alongside the velocity"
CLOSING_ATTEMPTS = 3

_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "dozen", "hundred", "thousand")
_CAUSAL = re.compile(r"\b(because|due to|caused?|causing|as a result|thanks to|led to|leading to|so that|owing to|therefore|which is why)\b", re.IGNORECASE)
_IDS = re.compile(r"\b(?:PM|RISK|sprint)-\d+\b", re.IGNORECASE)
# Neutral words a closing sentence may use beyond the facts' own: a comparison and a vague quantity, never a figure. Each is checked against the
# facts below, so "more work" is only allowed when the velocity fact says up, and "some items remain blocked" only when something is blocked.
_CLOSING_EXTRA = frozenset({"more", "less", "fewer", "work", "some", "same", "remain", "remains", "remained", "remaining", "week", "weeks"})


class FactLine(BaseModel):
    reference_id: str
    detail: str


class ClosingDraft(BaseModel):
    sentence: str


@dataclass
class Narrative:
    lines: list[FactualLine] = field(default_factory=list)  # grounded lines, in the facts' order
    dropped: list[dict] = field(default_factory=list)  # what grounding refused and why
    closing: str | None = None
    closing_refusals: list[str] = field(default_factory=list)  # why each refused closing attempt was refused
    facts: list[FactLine] = field(default_factory=list)  # what the model was handed

    def text_for(self, fact: FactLine) -> str:
        """The grounded line for a fact, or the recorded fact itself, marked, when its line was dropped."""
        for line in self.lines:
            if line.message_id == fact.reference_id:
                return line.text
        return f"{fact.detail} {_AS_RECORDED}"


def narrative_facts(facts: WeeklyFacts) -> list[FactLine]:
    """The facts the model may put into plain words, each one a sentence the report already states."""
    out: list[FactLine] = []
    if facts.completed_previous_week or facts.completed_this_week:
        this, before = len(facts.completed_this_week), len(facts.completed_previous_week)
        if facts.velocity_change_percent is None:
            detail = f"Completed this week: {this}. The week before: {before}."
        else:
            pct = facts.velocity_change_percent
            move = "unchanged" if pct == 0 else f"{'up' if pct > 0 else 'down'} {abs(pct)}%"
            detail = f"Completed this week: {this}, against {before} the week before: {move}."
        out.append(FactLine(reference_id="figure:velocity", detail=detail))
    out += [FactLine(reference_id=f"item:{a.item_id}", detail=f"{a.item_id} was added after planning ({a.detail}): {a.title}.") for a in facts.added_after_planning]
    out += [FactLine(reference_id=f"item:{b.item_id}", detail=f"{b.item_id} is blocked, {b.detail}: {b.title}.") for b in facts.blocked
            if f"item:{b.item_id}" not in {f.reference_id for f in out}]
    out += [FactLine(reference_id=f"item:{u.item_id}", detail=f"{u.item_id} has a status the tracker does not map ({u.detail}): {u.title}.") for u in facts.unmapped
            if f"item:{u.item_id}" not in {f.reference_id for f in out}]
    return out


def _facts_block(items: list[FactLine]) -> str:
    return "\n".join(f"{i}. reference_id: {item.reference_id}\n   detail: {item.detail}" for i, item in enumerate(items, start=1))


def _generate_lines(gateway, prompt, items: list[FactLine]) -> GroundingResult:
    lookup = {item.reference_id: item.detail for item in items}.get
    block = _facts_block(items)

    def generate_fn(feedback: str | None) -> list[FactualLine]:
        rendered = prompt.render(section_label=SECTION_LABEL, facts_block=block, feedback_block=f"\n{feedback}\n" if feedback else "")
        draft = generate_structured(gateway, rendered, MorningBriefSectionDraft, tool_name="weekly_narrative_lines")
        return [FactualLine(text=line.text, message_id=line.reference_id, quote=line.quote) for line in draft.lines]

    return ground_with_retry(generate_fn, lookup, content_check=_check_line_content)


def closing_problem(sentence: str, source: str) -> str | None:
    """Why this closing sentence may not be used, or None. The rules are code, so a model that ignores the prompt is refused, not trusted."""
    text = (sentence or "").strip()
    if not text:
        return "the sentence is empty"
    if len(re.findall(r"[.!?](?:\s|$)", text)) > 1:
        return "write exactly one sentence"
    if any(char.isdigit() for char in text):
        return "the sentence must contain no digit: every figure is already in the report; describe in words only"
    found = [w for w in _NUMBER_WORDS if re.search(rf"\b{w}\b", text, re.IGNORECASE)]
    if found:
        return f"the sentence must state no quantity, so do not write {', '.join(found)}: describe in words only"
    if _IDS.search(text):
        return "the sentence must not name an item or a risk"
    cause = _CAUSAL.search(text)
    if cause:
        return f"the sentence must not say why anything happened: do not write '{cause.group(0)}'"
    words = [w for w in _unsupported_words(text, source) if w.lower() not in _CLOSING_EXTRA]
    if words:
        return f"the sentence uses words that are in none of the facts: {', '.join(words)}. Use only the facts' own words and plain connecting words"
    return _claim_problem(text, source)


def _claim_problem(text: str, source: str) -> str | None:
    """Each thing the sentence says must be something the facts say."""
    lowered, facts = text.lower(), source.lower()
    says_more, says_less = bool(re.search(r"\bmore\b", lowered)), bool(re.search(r"\b(less|fewer)\b", lowered))
    if says_more and " up " not in f" {facts} ":
        return "the sentence says more work was completed, but the facts do not say the completed count is up"
    if says_less and " down " not in f" {facts} ":
        return "the sentence says less work was completed, but the facts do not say the completed count is down"
    if re.search(r"\bsame\b", lowered) and "unchanged" not in facts:
        return "the sentence says the same amount was completed, but the facts do not say it is unchanged"
    if re.search(r"\bblocked\b", lowered) and " is blocked" not in facts:
        return "the sentence mentions blocked items, but no fact says an item is blocked"
    if re.search(r"\badded\b", lowered) and "added after planning" not in facts:
        return "the sentence mentions items added, but no fact says an item was added after planning"
    if re.search(r"\b(not map|unmapped)", lowered) and "does not map" not in facts:
        return "the sentence mentions a status that is not mapped, but no fact says so"
    return None


def _generate_closing(gateway, prompt, items: list[FactLine]) -> tuple[str | None, list[str]]:
    block = _facts_block(items)
    source = " ".join(item.detail for item in items)
    refusals: list[str] = []
    feedback: str | None = None
    for _ in range(CLOSING_ATTEMPTS):
        rendered = prompt.render(facts_block=block, feedback_block=f"\nYour last sentence was refused: {feedback}\n" if feedback else "")
        draft = generate_structured(gateway, rendered, ClosingDraft, tool_name="weekly_narrative_closing")
        feedback = closing_problem(draft.sentence, source)
        if feedback is None:
            return draft.sentence.strip(), refusals
        refusals.append(feedback)
    return None, refusals


def generate_narrative(facts: WeeklyFacts, gateway, *, prompt_registry: PromptRegistry | None = None) -> Narrative:
    """The grounded lines and the closing sentence for `facts`. With nothing to rephrase, the model is not called at all."""
    items = narrative_facts(facts)
    if not items:
        return Narrative()
    registry = prompt_registry or PromptRegistry()
    result = _generate_lines(gateway, registry.get(NARRATIVE_CAPABILITY), items)
    closing, refusals = _generate_closing(gateway, registry.get(CLOSING_CAPABILITY), items)
    dropped = [{"reason": f.reason, "detail": f.detail, "text": f.line.text} for f in result.failures]
    return Narrative(lines=result.grounded_lines, dropped=dropped, closing=closing, closing_refusals=refusals, facts=items)
