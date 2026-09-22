"""
Morning brief expression (PM-08).

Facts in code (pm.reporting.facts, zero import of spine.llm/spine.prompts
anywhere in it), prose from the model here -- the same split proven in
P1 (src/p1/reporting/daily_summary.py: "facts in code, prose from the
model", via spine's own generate_structured + PromptRegistry). The model
is asked only to turn MorningBriefFacts into readable prose, schema-
constrained via generate_structured (Claude tool-use forced structured
output, never free-form JSON parsing) -- never to decide what counts as
a fact in the first place.

Beyond schema validation (which only checks SHAPE: is `narrative` a
string), this module adds its own grounding pass: every "PM-nnn" / "RISK-
nnn" id the returned narrative mentions must actually appear somewhere in
the facts it was given. A schema-valid narrative that invents an id
still fails this check and triggers a retry with the mismatch fed back
into the prompt -- mirroring P1's own grounding-kernel philosophy
(spine.grounding.kernel: never silently keep an ungrounded claim) without
reusing that kernel directly, since it's shaped specifically around
Teams message_id/quote verification, not our own item/risk ids.

Row-level acceptance test, literal: "Regenerating on the same snapshot
yields identical facts; wording may differ." See
tests/unit/test_reporting_morning_brief.py: the same MorningBriefFacts,
run through a fake gateway that returns two different narratives on two
successive calls, comes back with .facts identical both times while
.narrative is allowed -- expected -- to differ.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from pm.reporting.facts import MorningBriefFacts
from spine.llm.structured import generate_structured
from spine.prompts.registry import PromptRegistry

MORNING_BRIEF_CAPABILITY = "pm08_morning_brief"

_ITEM_ID_RE = re.compile(r"\bPM-\d{3}\b")
_RISK_ID_RE = re.compile(r"\bRISK-\d{3}\b")


class MorningBriefDraft(BaseModel):
    """The model's own schema-constrained output: a single narrative
    string. Deliberately not one field per section -- the facts already
    carry all the structure (sprint scope, per-person buckets, ranked
    blockers); the model's only job is prose, not re-deriving shape."""

    narrative: str


class GroundingError(RuntimeError):
    """Raised when the model's narrative still cites an item id or risk
    id that isn't actually present in the given facts, after every retry
    attempt has been spent. Never silently kept."""


class MorningBrief(BaseModel):
    facts: MorningBriefFacts
    narrative: str


def _known_ids(facts: MorningBriefFacts) -> tuple[set[str], set[str]]:
    """Every item id and risk id that legitimately appears anywhere in
    facts -- the whitelist _find_ungrounded_mentions() checks a
    narrative's own PM-nnn/RISK-nnn mentions against."""
    item_ids: set[str] = set()
    risk_ids: set[str] = set()
    for person in facts.people:
        for bucket in (person.delivered, person.pending, person.blocked):
            item_ids.update(item.item_id for item in bucket)
        item_ids.update(commitment.item_id for commitment in person.committed if commitment.item_id)
    for blocker in facts.blockers:
        risk_ids.add(blocker.risk_id)
        if blocker.related_item_id:
            item_ids.add(blocker.related_item_id)
    return item_ids, risk_ids


def _find_ungrounded_mentions(narrative: str, facts: MorningBriefFacts) -> list[str]:
    """Every PM-nnn/RISK-nnn id the narrative mentions that the facts
    never actually contained. Assignee ids aren't checked this way --
    free-text names have no fixed shape to regex for -- only item and
    risk ids, which do."""
    item_ids, risk_ids = _known_ids(facts)
    problems = [match for match in _ITEM_ID_RE.findall(narrative) if match not in item_ids]
    problems += [match for match in _RISK_ID_RE.findall(narrative) if match not in risk_ids]
    return problems


def _render_facts_block(facts: MorningBriefFacts) -> str:
    """Deterministic text rendering of the facts -- the model's ONLY
    source of truth; it is never handed the raw ProjectSnapshot, and
    never asked to look anything up itself."""
    lines: list[str] = []
    if facts.sprint is not None:
        lines.append(
            f"Sprint: {facts.sprint.display_name} ({facts.sprint.sprint_id}), "
            f"day {facts.sprint.day_number} of {facts.sprint.total_days} "
            f"({facts.sprint.start_date} to {facts.sprint.end_date}). "
            f"{facts.sprint.done_items} of {facts.sprint.total_items} items in this sprint are done."
        )
    else:
        lines.append("Sprint: no sprint on file covers this date.")
    lines.append("")
    lines.append("Per person:")
    for person in facts.people:
        committed = (
            "; ".join(f"{c.item_id or '(no item)'}: {c.text}" for c in person.committed) or "none"
        )
        delivered = ", ".join(f"{i.item_id} ({i.title})" for i in person.delivered) or "none"
        pending = ", ".join(f"{i.item_id} ({i.title})" for i in person.pending) or "none"
        blocked = ", ".join(f"{i.item_id} ({i.title})" for i in person.blocked) or "none"
        lines.append(
            f"- {person.assignee_id}: committed={committed} | delivered={delivered} "
            f"| pending={pending} | blocked={blocked}"
        )
    lines.append("")
    lines.append("Blockers, ranked by impact (highest first):")
    if facts.blockers:
        for blocker in facts.blockers:
            lines.append(
                f"- [{blocker.severity}] {blocker.risk_id}: {blocker.title} "
                f"(item {blocker.related_item_id or 'none'}, assignee {blocker.assignee_id or 'unassigned'})"
            )
    else:
        lines.append("- none")
    return "\n".join(lines)


def generate_morning_brief(
    facts: MorningBriefFacts,
    gateway,
    *,
    prompt_registry: PromptRegistry | None = None,
    max_attempts: int = 3,
) -> MorningBrief:
    """Turns `facts` into a MorningBrief via `gateway`. `facts` is never
    recomputed here -- this function's whole job is expression, not
    fact-gathering (see pm.reporting.facts for that half of the split).

    max_attempts bounds this function's OWN grounding retry loop, which
    is separate from (and sits on top of) generate_structured()'s own
    schema-shape retry budget: a schema-invalid response is retried
    inside generate_structured() itself (and raises StructuredOutputError
    if that budget is exhausted, uncaught here -- that failure mode is
    already "raise, don't default"); a schema-VALID response that still
    cites an unknown id is this module's own problem, retried here with
    the mismatch fed back into the prompt, and raises GroundingError of
    its own if this loop's budget runs out first."""
    registry = prompt_registry or PromptRegistry()
    prompt = registry.get(MORNING_BRIEF_CAPABILITY)
    facts_block = _render_facts_block(facts)

    problems: list[str] = []
    feedback_block = ""
    for _attempt in range(1, max_attempts + 1):
        rendered = prompt.render(facts_block=facts_block, feedback_block=feedback_block)
        draft = generate_structured(gateway, rendered, MorningBriefDraft, tool_name="morning_brief")
        problems = _find_ungrounded_mentions(draft.narrative, facts)
        if not problems:
            return MorningBrief(facts=facts, narrative=draft.narrative)
        feedback_block = (
            f"\nYour previous narrative mentioned {problems}, which do not appear anywhere "
            "in the facts above. Only reference ids that are explicitly listed there.\n"
        )

    raise GroundingError(
        f"morning brief narrative still referenced unknown id(s) after {max_attempts} attempts: {problems}"
    )
