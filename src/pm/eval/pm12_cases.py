"""
PM-12 golden cases: GC1 (citation rate) and GC2 (fabricated-claim count),
registered into spine's eval harness exactly the way P1 registers GC3/GC4
(see ../P3_Agents/src/p1/eval/chn15_cases.py).

GC1 -- of every line a model drafts on its FIRST attempt for the real
seeded morning brief (before the kernel's retry-and-drop safety net runs),
what proportion carries a reference_id that resolves? Measured with the
real spine.grounding.kernel.verify_lines against the brief's own real
reference lookup. Target >=0.90. The default gateway is a scripted stand-in
that drafts one faithful line per fact plus two hand-planted slips (one
line with no reference, one citing an item that does not exist), so the
number is a real measurement rather than a vacuous 1.0; pass a real
gateway_factory to measure an actual model instead.

GC2 -- of the lines and rendered text that survive into the FINAL brief,
how many claim something the facts do not support? A hard zero. The check
is re-derived here from `facts` itself, never from the kernel's own
bookkeeping (brief.dropped): every surviving line's reference must belong to
the SAME section's own fact set (a real item cited under the wrong section
is exactly as dishonest as an invented one), and every person's rendered
block must contain only text from lines legitimately theirs. Three
scenarios feed it: the real seeded brief with the model tampered on its
first attempt (cross-section leaks), the zero-activity assignee inside that
same brief, and an empty day where the model must not be called at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from spine.eval.cases import (
    GoldenCase,
    GoldenCaseRegistry,
    MetricResult,
    at_least,
    at_most,
)
from spine.grounding.kernel import FactualLine, verify_lines
from spine.llm.gateway import LLMResponse

from pm.adapters.code_host import CodeHostMock
from pm.adapters.commitments import CommitmentsMock
from pm.adapters.risk_log import RiskLogMock
from pm.adapters.teams import get_teams_reader
from pm.adapters.tracker import TrackerMock
from pm.eval.golden_cases import MORNING
from pm.reporting.facts import (
    MorningBriefFacts,
    PersonFacts,
    compute_morning_brief_facts,
)
from pm.reporting.morning_brief import (
    _AS_RECORDED,
    _NO_ACTIVITY_LINE,
    _SECTION_LABELS,
    SECTION_ORDER,
    MorningBrief,
    _build_reference_lookup,
    _reference,
    _section_facts,
    generate_morning_brief,
)
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import build_snapshot
from pm.storage.db import DEFAULT_DB_PATH

GC1_TARGET = 0.90
_ITEM_SECTIONS = ("committed", "delivered", "pending", "blocked")

_LABEL_TO_KEY = {label: key for key, label in _SECTION_LABELS.items()}
_SECTION_RE = re.compile(r'the "(?P<label>[^"]+)" section')
_FACT_RE = re.compile(r"^\d+\. reference_id: (?P<ref>\S+)\n\s+detail: (?P<detail>.*)$", re.MULTILINE)


def build_seeded_facts(db_path: str | Path | None = None) -> MorningBriefFacts:
    """The real morning-brief facts over this repo's own seeded database,
    as of golden case 3's MORNING moment (inside sprint-13)."""
    path = db_path if db_path is not None else DEFAULT_DB_PATH
    snapshot = build_snapshot(
        TrackerMock(db_path=path),
        CodeHostMock(db_path=path),
        get_teams_reader(db_path=path),
        CHANNEL_ID,
        risk_log=RiskLogMock(db_path=path),
        commitments_store=CommitmentsMock(db_path=path),
        taken_at=MORNING,
    )
    return compute_morning_brief_facts(snapshot)


def _empty_day_facts() -> MorningBriefFacts:
    return MorningBriefFacts(
        as_of=MORNING,
        sprint=None,
        people=[
            PersonFacts(assignee_id="quiet.person", committed=[], delivered=[], pending=[], blocked=[])
        ],
        blockers=[],
    )


def _prompt_section(prompt: str) -> str:
    match = _SECTION_RE.search(prompt)
    return _LABEL_TO_KEY[match.group("label")] if match else "unknown"


def _prompt_facts(prompt: str) -> list[tuple[str, str]]:
    facts_region = prompt.split("Items:", 1)[-1]
    return [(m.group("ref"), m.group("detail")) for m in _FACT_RE.finditer(facts_region)]


def _response(lines: list[dict]) -> LLMResponse:
    return LLMResponse(
        text=json.dumps({"lines": lines}),
        provider="scripted",
        model="scripted",
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0.0,
        cache_hit=False,
    )


class ScriptedGateway:
    """A faithful stand-in for a model: one line per fact in the prompt,
    reference echoed exactly, detail text as the prose. `tamper` maps a
    section key to a function that rewrites that section's FIRST-attempt
    lines (a forged or misplaced reference); every later attempt for that
    section is faithful again, as a model correcting itself on retry."""

    def __init__(self, tamper: dict[str, Callable[[list[dict]], list[dict]]] | None = None) -> None:
        self._tamper = tamper or {}
        self._attempts: dict[str, int] = {}
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        key = _prompt_section(prompt)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        lines = [{"text": detail, "reference_id": ref, "quote": detail} for ref, detail in _prompt_facts(prompt)]
        if self._attempts[key] == 1 and key in self._tamper:
            lines = self._tamper[key](lines)
        return _response(lines)


class RecordingGateway:
    """Wraps any gateway and records each section's FIRST-attempt draft."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.first_drafts: dict[str, list[dict]] = {}

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        response = self._inner.generate(prompt, **kwargs)
        key = _prompt_section(prompt)
        if key not in self.first_drafts:
            try:
                self.first_drafts[key] = list(json.loads(response.text).get("lines", []))
            except (ValueError, AttributeError):
                self.first_drafts[key] = []
        return response


def _default_gc1_gateway() -> ScriptedGateway:
    def no_reference(lines: list[dict]) -> list[dict]:
        if lines:
            lines[0] = {"text": lines[0]["text"], "reference_id": None}
        return lines

    def nonexistent_reference(lines: list[dict]) -> list[dict]:
        if lines:
            lines[0] = {"text": lines[0]["text"], "reference_id": "item:PM-999"}
        return lines

    return ScriptedGateway(tamper={"pending": no_reference, "delivered": nonexistent_reference})


def measure_gc1(
    facts: MorningBriefFacts | None = None,
    gateway_factory: Callable[[], object] = _default_gc1_gateway,
) -> list[MetricResult]:
    facts = facts or build_seeded_facts()
    recorder = RecordingGateway(gateway_factory())
    generate_morning_brief(facts, recorder)

    drafted = [
        FactualLine(text=line.get("text", ""), message_id=line.get("reference_id"))
        for lines in recorder.first_drafts.values()
        for line in lines
    ]
    total = len(drafted)
    grounded = len(verify_lines(drafted, _build_reference_lookup(facts)).grounded_lines)
    rate = grounded / total if total else 1.0
    return [
        MetricResult(
            metric_id="GC1-citation-rate",
            name="first-attempt brief lines with a resolvable reference",
            measured=round(rate, 4),
            target=GC1_TARGET,
            comparator_name="at_least",
            passed=at_least(rate, GC1_TARGET),
            detail=f"{grounded} of {total} first-attempt lines resolve",
        )
    ]


# --- GC2 -------------------------------------------------------------------


def _section_refs(facts: MorningBriefFacts) -> dict[str, set[str]]:
    return {key: {item.reference_id for item in items} for key, items in _section_facts(facts).items()}


def _person_refs(person: PersonFacts) -> dict[str, set[str]]:
    return {
        "committed": {
            _reference("item", c.item_id) if c.item_id else _reference("commitment", str(c.id))
            for c in person.committed
        },
        "delivered": {_reference("item", i.item_id) for i in person.delivered},
        "pending": {_reference("item", i.item_id) for i in person.pending},
        "blocked": {_reference("item", i.item_id) for i in person.blocked},
    }


def _bucket_problem(rendered: str | None, grounded: list[str], refs: set[str], assignee: str) -> str | None:
    """Independent check of one rendered bucket: grounded lines appear as
    written; anything else is a fact shown as recorded (marked, naming this
    person); "none." only when the bucket truly has no facts."""
    if rendered is None:
        return "is missing"
    if not refs:
        return None if rendered == "none." else f"should be 'none.' but is {rendered!r}"
    if rendered == "none.":
        return "says 'none.' although it has facts"
    rest = rendered
    for text in grounded:
        if text not in rest:
            return f"lost a grounded line {text!r}"
        rest = rest.replace(text, "", 1)
    *marked, tail = rest.split(_AS_RECORDED)
    if tail.strip() or (not marked and not grounded):
        return f"has text that is neither a grounded line nor a marked fact: {rest.strip()!r}"
    for segment in marked:
        if assignee not in segment:
            return f"has a marked fact that does not name {assignee}: {segment.strip()!r}"
    return None


def _person_block(content: str, assignee_id: str) -> list[str]:
    lines = content.split("\n")
    header = f"## {assignee_id}"
    if header not in lines:
        return []
    block = []
    for line in lines[lines.index(header) + 1 :]:
        if line == "":
            break
        block.append(line)
    return block


def count_fabrications(brief: MorningBrief, facts: MorningBriefFacts) -> list[str]:
    """Independent of the kernel: re-derived straight from `facts`."""
    problems: list[str] = []
    legit = _section_refs(facts)

    for key in SECTION_ORDER:
        for line in brief.sections[key]:
            if line.message_id not in legit[key]:
                problems.append(f"{key}: line cites {line.message_id!r}, which is not one of this section's facts")

    for person in facts.people:
        block = _person_block(brief.content, person.assignee_id)
        if not person.has_activity:
            if block != [f"- {_NO_ACTIVITY_LINE}"]:
                problems.append(f"{person.assignee_id}: zero-activity person rendered as {block!r}")
            continue
        refs = _person_refs(person)
        for label, key in (
            ("Committed", "committed"),
            ("Delivered", "delivered"),
            ("Pending", "pending"),
            ("Blocked", "blocked"),
        ):
            prefix = f"- {label}: "
            rendered = next((b[len(prefix):] for b in block if b.startswith(prefix)), None)
            grounded = [line.text for line in brief.sections[key] if line.message_id in refs[key]]
            problem = _bucket_problem(rendered, grounded, refs[key], person.assignee_id)
            if problem:
                problems.append(f"{person.assignee_id}: {label} bucket {problem}")

    blocker_texts = {line.text for line in brief.sections["blockers"]}
    in_blockers = False
    for rendered_line in brief.content.split("\n"):
        if rendered_line == "## Blockers":
            in_blockers = True
        elif (
            in_blockers
            and rendered_line.startswith("- ")
            and rendered_line != "- none."
            and rendered_line[2:] not in blocker_texts
            and not (
                rendered_line.endswith(_AS_RECORDED)
                and any(b.risk_id in rendered_line for b in facts.blockers)
            )
        ):
            problems.append("blockers: rendered line is neither a grounded blocker line nor a marked fact")
    if facts.blockers and "- none." in brief.content.split("## Blockers")[1]:
        problems.append("blockers: rendered 'none.' although blockers exist")
    return problems


def _first_foreign_ref(facts: MorningBriefFacts, key: str) -> str | None:
    own = _section_refs(facts)[key]
    for other_key, refs in _section_refs(facts).items():
        if other_key == key or other_key not in _ITEM_SECTIONS:
            continue
        for ref in sorted(refs - own):
            return ref
    return None


def _cross_section_tamper(facts: MorningBriefFacts) -> dict[str, Callable[[list[dict]], list[dict]]]:
    tamper: dict[str, Callable[[list[dict]], list[dict]]] = {}
    for key in ("delivered", "pending", "blocked"):
        foreign = _first_foreign_ref(facts, key)
        if foreign is None:
            continue

        def rewrite(lines: list[dict], foreign: str = foreign) -> list[dict]:
            if lines:
                lines[0] = {"text": lines[0]["text"], "reference_id": foreign}
            return lines

        tamper[key] = rewrite
    return tamper


def measure_gc2(facts: MorningBriefFacts | None = None) -> list[MetricResult]:
    facts = facts or build_seeded_facts()
    problems: list[str] = []

    seeded_gateway = ScriptedGateway(tamper=_cross_section_tamper(facts))
    seeded_brief = generate_morning_brief(facts, seeded_gateway)
    seeded_problems = count_fabrications(seeded_brief, facts)
    problems += [f"seeded: {p}" for p in seeded_problems]

    empty_facts = _empty_day_facts()
    empty_gateway = ScriptedGateway()
    empty_brief = generate_morning_brief(empty_facts, empty_gateway)
    empty_problems = count_fabrications(empty_brief, empty_facts)
    if empty_gateway.calls:
        empty_problems.append(f"model called {empty_gateway.calls} time(s) on a day with no facts")
    problems += [f"empty day: {p}" for p in empty_problems]

    zero_activity = sum(1 for person in facts.people if not person.has_activity)
    return [
        MetricResult(
            metric_id="GC2-fabricated-claim-count",
            name="fabricated claims surviving into the brief (hard zero)",
            measured=len(problems),
            target=0,
            comparator_name="at_most",
            passed=at_most(len(problems), 0),
            detail=(
                f"{len(seeded_brief.sections['delivered']) + len(seeded_brief.sections['pending'])} "
                f"delivered/pending lines checked, {zero_activity} zero-activity person(s), "
                f"empty day gateway calls={empty_gateway.calls}"
                + (f"; first problem: {problems[0]}" if problems else "")
            ),
        )
    ]


def register(
    registry: GoldenCaseRegistry,
    *,
    db_path: str | Path | None = None,
    gateway_factory: Callable[[], object] = _default_gc1_gateway,
) -> None:
    registry.register(
        GoldenCase(
            case_id="GC1",
            description="Citation rate: first-attempt brief lines carrying a resolvable reference",
            measure_fn=lambda: measure_gc1(build_seeded_facts(db_path), gateway_factory),
        )
    )
    registry.register(
        GoldenCase(
            case_id="GC2",
            description="Fabrication probe: no claim survives that the facts do not support",
            measure_fn=lambda: measure_gc2(build_seeded_facts(db_path)),
        )
    )
