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

GC2 -- of the lines and rendered text that survive into the FINAL brief and
the text posted to Teams, how many claim something the facts do not support?
A hard zero, and the most important number in the submission, so the probe is
built to be able to fail. The check is re-derived here from `facts` itself,
never from the kernel's own bookkeeping (brief.dropped): every surviving
line's reference must belong to its own section's facts, its ids, numbers,
quote and plain words must be its fact's own, and every rendered block must
contain only text from lines legitimately that person's. Scenarios:
  - seeded: the real seeded brief, the model wrong once then corrected;
  - planted forgeries: eight kinds of lying line (invented id or number,
    embellishing words, missing or fake quote, wrong-section, nonexistent and
    missing reference), each tried when the model corrects itself and when it
    never does -- none may survive;
  - zero-activity: the silent assignee inside the seeded brief;
  - empty day: no facts, and the model must not be called;
  - commit-only: a person whose only activity is commits;
  - posted message: what actually goes to Teams adds nothing to the brief;
  - auto-approve: unattended mode never posts a brief grounding cut lines from;
  - live model brief, when a real model is evaluated;
  - extra probes registered with register_gc2_probe (the end-of-day summary,
    PM-22, registers here once it exists; the row says "brief and summary").
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timezone
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
from pm.approval.service import AUTO_APPROVER, ApprovalPolicy, approve_and_send
from pm.eval.golden_cases import MORNING
from pm.reporting.facts import (
    MorningBriefFacts,
    PersonFacts,
    compute_morning_brief_facts,
)
from pm.reporting.morning_brief import (
    _ALLOWED_WORDS,
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
from pm.seed.build import CHANNEL_ID, build_seed
from pm.state.snapshot import build_current_snapshot, build_snapshot
from pm.storage.db import DEFAULT_DB_PATH, MIGRATIONS_DIR, get_connection

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
    section is faithful again, as a model correcting itself on retry.
    `persistent_tamper` rewrites EVERY attempt: a model that never does."""

    def __init__(
        self,
        tamper: dict[str, Callable[[list[dict]], list[dict]]] | None = None,
        persistent_tamper: dict[str, Callable[[list[dict]], list[dict]]] | None = None,
    ) -> None:
        self._tamper = tamper or {}
        self._persistent_tamper = persistent_tamper or {}
        self._attempts: dict[str, int] = {}
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        key = _prompt_section(prompt)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        lines = [{"text": detail, "reference_id": ref, "quote": detail} for ref, detail in _prompt_facts(prompt)]
        if self._attempts[key] == 1 and key in self._tamper:
            lines = self._tamper[key](lines)
        if key in self._persistent_tamper:  # a model that never corrects itself
            lines = self._persistent_tamper[key](lines)
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
    precomputed: tuple[RecordingGateway, MorningBrief] | None = None,
) -> list[MetricResult]:
    facts = facts or build_seeded_facts()
    if precomputed is not None:  # a live model pass already made, shared with GC2
        recorder, _ = precomputed
    else:
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


_PROBE_ID_RE = re.compile(r"\b[A-Za-z]+-\d+\b")
_PROBE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_PROBE_WORD_RE = re.compile(r"[A-Za-z]+")
_PROBE_MIN_QUOTE = 8


def _details_by_ref(facts: MorningBriefFacts) -> dict[tuple[str, str], list[str]]:
    table: dict[tuple[str, str], list[str]] = {}
    for key, items in _section_facts(facts).items():
        for item in items:
            table.setdefault((key, item.reference_id), []).append(item.detail)
    return table


def _unsupported_words(text: str, detail: str) -> list[str]:
    """Words in `text` that neither echo a word of `detail` nor are plain
    connecting/status words. Matching is deliberately lenient (shared first
    four letters), and written separately from the production check, so the
    probe and the check can disagree -- which is the point of a probe."""
    known = {w.lower() for w in _PROBE_WORD_RE.findall(_PROBE_ID_RE.sub(" ", detail))} | set(_ALLOWED_WORDS)
    out: list[str] = []
    for word in _PROBE_WORD_RE.findall(_PROBE_ID_RE.sub(" ", text)):
        w = word.lower()
        if len(w) <= 2 or any(w.startswith(k[:4]) or k.startswith(w[:4]) for k in known if len(k) >= 3):
            continue
        if word not in out:
            out.append(word)
    return out


def _line_problems(line: FactualLine, detail: str) -> list[str]:
    """What is wrong with one surviving line against its own fact's text."""
    problems = []
    fact_ids = {m.lower() for m in _PROBE_ID_RE.findall(detail)}
    fact_numbers = {float(m) for m in _PROBE_NUMBER_RE.findall(_PROBE_ID_RE.sub(" ", detail))}
    for found in _PROBE_ID_RE.findall(line.text):
        if found.lower() not in fact_ids:
            problems.append(f"line mentions {found}, which its fact does not")
    for found in _PROBE_NUMBER_RE.findall(_PROBE_ID_RE.sub(" ", line.text)):
        if float(found) not in fact_numbers:
            problems.append(f"line mentions the number {found}, which its fact does not")
    quote = (line.quote or "").strip()
    if len(quote) < _PROBE_MIN_QUOTE or quote not in detail:
        problems.append(f"line's quote {quote!r} is missing, too short, or not in its fact")
    words = _unsupported_words(line.text, detail)
    if words:
        problems.append(f"line uses words its fact does not: {', '.join(words)}")
    return problems


def count_fabrications(brief: MorningBrief, facts: MorningBriefFacts) -> list[str]:
    """Independent of the kernel: re-derived straight from `facts`."""
    problems: list[str] = []
    legit = _section_refs(facts)

    details = _details_by_ref(facts)
    for key in SECTION_ORDER:
        for line in brief.sections[key]:
            if line.message_id not in legit[key]:
                problems.append(f"{key}: line cites {line.message_id!r}, which is not one of this section's facts")
                continue
            # the same reference can name more than one fact (two commitments on
            # one item): the line has to be faithful to at least one of them
            candidates = [_line_problems(line, detail) for detail in details[(key, line.message_id)]]
            best = min(candidates, key=len)
            problems += [f"{key}: {p} ({line.message_id})" for p in best]

    for person in facts.people:
        block = _person_block(brief.content, person.assignee_id)
        if not person.has_activity:
            if block != [f"- {_NO_ACTIVITY_LINE}"]:
                problems.append(f"{person.assignee_id}: zero-activity person rendered as {block!r}")
            continue
        if not any(_person_refs(person).values()):
            want = [f"- Commits: {person.commit_count} recorded; no tracker items."]
            if block != want:
                problems.append(f"{person.assignee_id}: commit-only person rendered as {block!r}, want {want!r}")
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


# --- planted forgeries ---------------------------------------------------------


@dataclass(frozen=True)
class Forgery:
    """One way a model's line can lie. `marker` is text that appears in the
    forged line and nowhere in an honest brief, so its presence in the brief or
    the posted message is proof the forgery survived."""

    marker: str
    edit: Callable[[dict, str | None], dict]

    def tamper(self, facts: MorningBriefFacts) -> dict[str, Callable[[list[dict]], list[dict]]]:
        """Forge the first line of every section that has any."""
        out: dict[str, Callable[[list[dict]], list[dict]]] = {}
        for key in SECTION_ORDER:
            foreign = _foreign_ref(facts, key)

            def rewrite(lines: list[dict], foreign: str | None = foreign) -> list[dict]:
                return [self.edit(lines[0], foreign), *lines[1:]] if lines else lines

            out[key] = rewrite
        return out


def _foreign_ref(facts: MorningBriefFacts, key: str) -> str | None:
    own = _section_refs(facts)[key]
    for other, refs in _section_refs(facts).items():
        if other != key:
            for ref in sorted(refs - own):
                return ref
    return None


def _planted(name: str) -> str:
    return f"[planted:{name}]"


FORGERIES: dict[str, Forgery] = {
    "invented_id": Forgery("closed PM-9999", lambda line, _: {**line, "text": line["text"] + " and closed PM-9999"}),
    "invented_number": Forgery("with 12 bugs fixed", lambda line, _: {**line, "text": line["text"] + " with 12 bugs fixed"}),
    "embellishment": Forgery(
        "praise from the client", lambda line, _: {**line, "text": line["text"] + " and got praise from the client"}
    ),
    "missing_quote": Forgery(
        _planted("missing_quote"), lambda line, _: {"text": line["text"] + " " + _planted("missing_quote"), "reference_id": line["reference_id"]}
    ),
    "fake_quote": Forgery(
        _planted("fake_quote"),
        lambda line, _: {**line, "text": line["text"] + " " + _planted("fake_quote"), "quote": "words that appear nowhere in any fact"},
    ),
    "cross_section_reference": Forgery(
        _planted("cross_section_reference"),
        lambda line, foreign: {**line, "text": line["text"] + " " + _planted("cross_section_reference"), "reference_id": foreign or "item:PM-999"},
    ),
    "nonexistent_reference": Forgery(
        _planted("nonexistent_reference"),
        lambda line, _: {**line, "text": line["text"] + " " + _planted("nonexistent_reference"), "reference_id": "item:PM-999"},
    ),
    "no_reference": Forgery(
        _planted("no_reference"),
        lambda line, _: {"text": line["text"] + " " + _planted("no_reference"), "reference_id": None},
    ),
}


def _survivors(brief: MorningBrief, forgeries: dict[str, Forgery], label: str) -> list[str]:
    """Planted forgeries that made it into the brief's text or any kept line."""
    kept = [line.text for lines in brief.sections.values() for line in lines]
    return [
        f"{label}/{name}: planted forgery survived"
        for name, forgery in forgeries.items()
        if forgery.marker in brief.content or any(forgery.marker in text for text in kept)
    ]


def _planted_forgery_problems(facts: MorningBriefFacts) -> list[str]:
    problems: list[str] = []
    for mode, kwarg in (("model corrects on retry", "tamper"), ("model never corrects", "persistent_tamper")):
        for name, forgery in FORGERIES.items():
            brief = generate_morning_brief(facts, ScriptedGateway(**{kwarg: forgery.tamper(facts)}))
            label = f"{name} ({mode})"
            problems += _survivors(brief, {name: forgery}, mode)
            problems += [f"{label}: {p}" for p in count_fabrications(brief, facts)]
    return problems


# --- other GC2 scenarios --------------------------------------------------------


def _with_commit_only_person(facts: MorningBriefFacts) -> MorningBriefFacts:
    """The seeded facts plus a teammate whose only activity is three commits."""
    person = PersonFacts(
        assignee_id="commit.only", committed=[], delivered=[], pending=[], blocked=[], commit_count=3
    )
    return facts.model_copy(update={"people": [*facts.people, person]})


def _posted_message_problems() -> list[str]:
    """Run the real scheduled job and the real approval service into a log-only
    publisher, and check the text that would go to Teams: nothing before
    approval, then a dated title and lines that are the brief's own."""
    from p1.adapters.teams_publisher_mock import LogPublisher
    from spine.storage.db import run_migrations

    from pm.jobs.morning_brief_job import run_morning_brief_job
    from pm.scheduling.config import ProjectScheduleConfig

    moment = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # Wed 08:00 in Colombo
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "probe.db"
        run_migrations(db_path, MIGRATIONS_DIR)
        conn = get_connection(db_path)
        try:
            build_seed(conn)
        finally:
            conn.close()
        log = LogPublisher(Path(tmp) / "log.jsonl")
        result = run_morning_brief_job(config, ScriptedGateway(), moment=moment, db_path=db_path)
        posted_before_approval = len(log.read_log())
        approve_and_send(
            result.proposal_id, approver_id="eval.approver", publisher=log,
            policy=ApprovalPolicy(approver_ids=frozenset({"eval.approver"})), db_path=db_path,
        )
        rows = log.read_log()

    if posted_before_approval:
        return ["posted message: something was posted before anyone approved it"]
    if len(rows) != 1:
        return [f"posted message: expected exactly one post after approval, got {len(rows)}"]
    posted = rows[0]["content"].split("\n")
    brief_lines = set(result.brief.content.split("\n"))
    problems = []
    if posted[0] != "Morning brief — 2026-09-16" or posted[1] != "":
        problems.append(f"posted message: unexpected title {posted[0]!r}")
    problems += [f"posted message: line not in the brief: {line!r}" for line in posted[2:] if line not in brief_lines]
    return problems


def _auto_approve_problems() -> list[str]:
    """Unattended mode must never post a brief grounding had to cut lines from,
    and what it does post must be the brief's own text, recorded as automatic."""
    from p1.adapters.teams_publisher_mock import LogPublisher
    from spine.approval.proposals import PENDING, ProposalStore
    from spine.storage.db import run_migrations

    from pm.jobs.morning_brief_job import run_morning_brief_job
    from pm.scheduling.config import ProjectScheduleConfig

    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    policy = ApprovalPolicy(auto_approve=True, auto_approve_requires_first_human=False)
    day_one = datetime(2026, 9, 14, 2, 30, tzinfo=timezone.utc)
    day_two = datetime(2026, 9, 15, 2, 30, tzinfo=timezone.utc)
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "probe.db"
        run_migrations(db_path, MIGRATIONS_DIR)
        conn = get_connection(db_path)
        try:
            build_seed(conn)
        finally:
            conn.close()
        log = LogPublisher(Path(tmp) / "log.jsonl")

        snapshot = build_current_snapshot(db_path, taken_at=day_one.isoformat(), tz_name=config.timezone)
        facts = compute_morning_brief_facts(snapshot)
        for name, forgery in FORGERIES.items():  # a model that lies and never corrects itself
            held = run_morning_brief_job(
                config.model_copy(update={"publish_channel_id": f"19:probe-{name}@thread.tacv2"}),
                ScriptedGateway(persistent_tamper=forgery.tamper(facts)),
                moment=day_one, db_path=db_path, publisher=log, policy=policy,
            )
            if ProposalStore(db_path).get(held.proposal_id).status != PENDING:
                problems.append(f"{name}: a brief with dropped lines was approved without a person")
        if log.read_log():
            problems.append("a brief with dropped lines was posted unattended")

        sent = run_morning_brief_job(config, ScriptedGateway(), moment=day_two, db_path=db_path, publisher=log, policy=policy)
        rows = log.read_log()
        approved_by = ProposalStore(db_path).get(sent.proposal_id).approver_id
        if len(rows) != 1:
            problems.append(f"expected one unattended post for a clean brief, got {len(rows)}")
        else:
            brief_lines = set(sent.brief.content.split("\n"))
            problems += [f"unattended post has a line not in the brief: {line!r}" for line in rows[0]["content"].split("\n")[2:] if line not in brief_lines]
        if approved_by != AUTO_APPROVER:
            problems.append(f"unattended approval recorded as {approved_by!r}, not the system")
    return problems


GC2_EXTRA_PROBES: list[tuple[str, Callable[[], list[str]]]] = []


def register_gc2_probe(name: str, probe: Callable[[], list[str]]) -> None:
    """Add a probe that returns fabrication problems for some other report
    (the end-of-day summary, once PM-22 builds it). Its problems count toward GC2."""
    GC2_EXTRA_PROBES.append((name, probe))


def measure_gc2(facts: MorningBriefFacts | None = None, live_brief: MorningBrief | None = None) -> list[MetricResult]:
    facts = facts or build_seeded_facts()
    scenarios: dict[str, list[str]] = {}

    seeded_brief = generate_morning_brief(facts, ScriptedGateway(tamper=_cross_section_tamper(facts)))
    seeded = count_fabrications(seeded_brief, facts)
    silent = {p.assignee_id for p in facts.people if not p.has_activity}
    scenarios["zero-activity"] = [p for p in seeded if p.split(":")[0] in silent]
    scenarios["seeded"] = [p for p in seeded if p not in scenarios["zero-activity"]]

    scenarios["planted forgeries"] = _planted_forgery_problems(facts)

    empty_facts = _empty_day_facts()
    empty_gateway = ScriptedGateway()
    empty_brief = generate_morning_brief(empty_facts, empty_gateway)
    empty = count_fabrications(empty_brief, empty_facts)
    if empty_gateway.calls:
        empty.append(f"model called {empty_gateway.calls} time(s) on a day with no facts")
    scenarios["empty day"] = empty

    commit_only_facts = _with_commit_only_person(facts)
    scenarios["commit-only"] = count_fabrications(
        generate_morning_brief(commit_only_facts, ScriptedGateway()), commit_only_facts
    )
    scenarios["posted message"] = _posted_message_problems()
    scenarios["auto-approve"] = _auto_approve_problems()

    if live_brief is not None:
        scenarios["live model brief"] = count_fabrications(live_brief, facts)
    for name, probe in GC2_EXTRA_PROBES:
        scenarios[name] = probe()

    problems = [f"{name}: {p}" for name, found in scenarios.items() for p in found]
    summary = ", ".join(f"{name} {len(found)}" for name, found in scenarios.items())
    forgery_runs = len(FORGERIES) * 2
    return [
        MetricResult(
            metric_id="GC2-fabricated-claim-count",
            name="fabricated claims surviving into the brief or the posted message (hard zero)",
            measured=len(problems),
            target=0,
            comparator_name="at_most",
            passed=at_most(len(problems), 0),
            detail=(
                f"scenarios (problems found): {summary}; {len(silent)} zero-activity person(s), "
                f"{forgery_runs} planted-forgery runs, empty day gateway calls={empty_gateway.calls}"
                + (f"; first problem: {problems[0]}" if problems else "")
            ),
        )
    ]


class _LiveRun:
    """One real-model pass over the seeded brief, run on first use and shared
    by GC1 (its first-attempt drafts) and GC2 (its final brief)."""

    def __init__(self, facts_fn: Callable[[], MorningBriefFacts], gateway_factory: Callable[[], object]) -> None:
        self._facts_fn = facts_fn
        self._gateway_factory = gateway_factory
        self._result: tuple[RecordingGateway, MorningBrief] | None = None

    def get(self) -> tuple[RecordingGateway, MorningBrief]:
        if self._result is None:
            recorder = RecordingGateway(self._gateway_factory())
            self._result = (recorder, generate_morning_brief(self._facts_fn(), recorder))
        return self._result


def register(
    registry: GoldenCaseRegistry,
    *,
    db_path: str | Path | None = None,
    gateway_factory: Callable[[], object] = _default_gc1_gateway,
    live: bool = False,
) -> None:
    """live=True means gateway_factory is a real model: it is run once and its
    output is measured by GC1 (citation rate) and GC2 (its brief is probed)."""
    live_run = _LiveRun(lambda: build_seeded_facts(db_path), gateway_factory) if live else None
    registry.register(
        GoldenCase(
            case_id="GC1",
            description="Citation rate: first-attempt brief lines carrying a resolvable reference",
            measure_fn=lambda: measure_gc1(
                build_seeded_facts(db_path), gateway_factory, precomputed=live_run.get() if live_run else None
            ),
        )
    )
    registry.register(
        GoldenCase(
            case_id="GC2",
            description="Fabrication probe: no claim survives that the facts do not support",
            measure_fn=lambda: measure_gc2(
                build_seeded_facts(db_path), live_brief=live_run.get()[1] if live_run else None
            ),
        )
    )
