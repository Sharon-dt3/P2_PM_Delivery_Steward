"""PM-22: the end-of-day summary: what shipped, what is still pending, what is newly blocked, and what
else changed since the morning, built from the stored diff and worded by a model.

Facts in code, prose from the model, exactly as the morning brief does it (pm.reporting.morning_brief):

- pm.reporting.end_of_day_facts decides WHICH items changed and which section each is in;
- for each section that has something to say, the model is shown only that section's facts and asked
  for one line per fact; every line must cite its own fact (reference-or-drop), carry a verbatim quote,
  and use only words, ids and numbers the fact contains (the same checks the brief uses);
- a line that fails is retried and then dropped, and the change is shown as the recorded fact itself,
  marked [as recorded], so nothing that changed is ever lost to a model failure and nothing the model
  invented is ever shown;
- the model is asked nothing at all when nothing changed.

Each item appears at most once in the whole summary: a model that repeats a line does not make an
item appear twice.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from spine.grounding.kernel import (
    FactualLine,
    GroundingFailure,
    GroundingResult,
    ground_with_retry,
)
from spine.llm.structured import generate_structured
from spine.prompts.registry import PromptRegistry

from pm.reporting.end_of_day_facts import (
    NEWLY_BLOCKED,
    OTHER_CHANGES,
    SECTION_ORDER,
    SHIPPED,
    STILL_PENDING,
    EndOfDayFacts,
)
from pm.reporting.morning_brief import (
    _AS_RECORDED,
    MorningBriefSectionDraft,
    _check_line_content,
    _FactLine,
    _render_facts_block,
)
from pm.state.moments import parse_moment

logger = logging.getLogger(__name__)

SUMMARY_CAPABILITY = "pm22_end_of_day_summary"

SECTION_TITLES = {
    SHIPPED: "What shipped",
    STILL_PENDING: "What is still pending",
    NEWLY_BLOCKED: "What is newly blocked",
    OTHER_CHANGES: "What else changed since morning",
}
_SECTION_LABELS = {key: title[0].lower() + title[1:] for key, title in SECTION_TITLES.items()}


class EndOfDaySummary(BaseModel):
    facts: EndOfDayFacts
    sections: dict[str, list[FactualLine]]  # grounded lines, keyed by section
    dropped: dict[str, list[dict]]  # what grounding refused, keyed by section
    content: str


def _reference(item_id: str) -> str:
    return f"item:{item_id}"


def _section_lines(gateway, prompt, facts: EndOfDayFacts, key: str) -> GroundingResult:
    items = facts.sections[key]
    if not items:
        return GroundingResult()
    fact_lines = [_FactLine(reference_id=_reference(item.item_id), detail=item.detail) for item in items]
    lookup = {line.reference_id: line.detail for line in fact_lines}.get
    facts_block = _render_facts_block(fact_lines)

    def generate_fn(feedback: str | None) -> list[FactualLine]:
        rendered = prompt.render(
            section_label=_SECTION_LABELS[key], facts_block=facts_block, feedback_block=f"\n{feedback}\n" if feedback else "",
        )
        draft = generate_structured(gateway, rendered, MorningBriefSectionDraft, tool_name="end_of_day_section")
        return [FactualLine(text=line.text, message_id=line.reference_id, quote=line.quote) for line in draft.lines]

    try:
        return ground_with_retry(generate_fn, lookup, content_check=_check_line_content)
    except Exception as exc:  # noqa: BLE001 - a model that fails must not lose a change that really happened
        logger.warning("eod_section_failed section=%s error=%s: %s", key, type(exc).__name__, exc)
        return GroundingResult(failures=[_model_failure(exc)])


def _model_failure(exc: Exception) -> GroundingFailure:
    return GroundingFailure(line=FactualLine(text="", message_id=None, quote=None), reason="model_failed",
                            detail=f"{type(exc).__name__}: {exc}")


def _local_time(stamp: str, tz: str) -> str:
    return parse_moment(stamp).astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _worded(item, text: str | None) -> str:
    """The line for one change: the model's words if they were accepted (with the item's id put in front
    when the model named it by title alone, so every line says which item it is about, once), otherwise the
    recorded fact, marked as such."""
    if text is None:
        return f"{item.detail} {_AS_RECORDED}"
    return text if item.item_id in text else f"{item.item_id}: {text}"


def _render(facts: EndOfDayFacts, sections: dict[str, list[FactualLine]]) -> str:
    """Deterministic assembly: no model call happens here. A change whose line was dropped for good is
    shown as the recorded fact, marked [as recorded]; "none." appears only when nothing changed in a section."""
    parts = [
        (
            f"Changes since the morning snapshot ({_local_time(facts.morning_taken_at, facts.timezone)}) "
            f"to the end-of-day snapshot ({_local_time(facts.evening_taken_at, facts.timezone)}), {facts.local_date}."
        )
    ]
    if facts.morning_source != "stored":
        parts.append("No morning snapshot was stored for this day: the morning state was rebuilt from the tracker's history.")
    parts.append("")
    for key in SECTION_ORDER:
        parts.append(f"## {SECTION_TITLES[key]}")
        grounded: dict[str, str] = {}
        for line in sections[key]:
            if line.message_id:
                grounded.setdefault(line.message_id, line.text)  # one line per item, however often the model said it
        lines = [f"- {_worded(item, grounded.get(_reference(item.item_id)))}" for item in facts.sections[key]]
        parts.extend(lines or ["- none."])
        parts.append("")
    count = facts.unchanged_open_count
    parts.append(f"{_plural(count, 'other open item', 'other open items')} did not change today.")
    if facts.unchanged_unmapped:  # PM-31: not counted as open (or as anything else). Counted, not named: this summary names only what changed (PM-22)
        n = len(facts.unchanged_unmapped)
        parts.append(f"{_plural(n, 'other item has', 'other items have')} a status the tracker does not map (UNMAPPED) and {'is' if n == 1 else 'are'} not counted as open.")
    return "\n".join(parts)


def generate_end_of_day_summary(
    facts: EndOfDayFacts, gateway, *, prompt_registry: PromptRegistry | None = None
) -> EndOfDaySummary:
    """Turns `facts` into a grounded summary via `gateway`. `facts` is never recomputed here."""
    prompt = (prompt_registry or PromptRegistry()).get(SUMMARY_CAPABILITY)
    sections: dict[str, list[FactualLine]] = {}
    dropped: dict[str, list[dict]] = {}
    for key in SECTION_ORDER:
        result = _section_lines(gateway, prompt, facts, key)
        sections[key] = result.grounded_lines
        dropped[key] = [{"reason": f.reason, "detail": f.detail, "text": f.line.text} for f in result.failures]
    return EndOfDaySummary(facts=facts, sections=sections, dropped=dropped, content=_render(facts, sections))
