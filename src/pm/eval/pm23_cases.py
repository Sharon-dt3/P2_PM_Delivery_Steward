"""PM-23 golden case 9: the facts of the morning brief are deterministic; only the wording may differ.

The brief is generated twice from the same snapshot (the second time from the stored copy of it, as the
job reads it back), by two models that word every line differently. The WORDING may differ. What a reader
takes from the brief may not: the set of items, who owns them, the counts, and each item's status. This
case reads that fact set out of each rendered brief, from the text a person would read and with a parser
that shares no code with the generator, and compares the two. Zero divergences is the acceptance.

The fact set (each a tuple, so two sets can be diffed exactly):

  sprint       its id, day N of M, and "X of Y items done"
  owner        every person with a section, and "no update" for the one with nothing
  item         (owner, status bucket, item id): which items each person has delivered, pending, blocked
  count        (owner, bucket, how many), the counts a reader would quote
  due          (owner, a commitment's due date), for the commitments that have one: read as a date wherever the line puts it
  blocker      (rank, risk id) and its severity: the brief guarantees both on every blocker line, whatever the wording

Three further numbers keep a clean sheet honest:

  GC9-snapshot-fact-gap-count        each generation also matches the facts computed from the snapshot (structured,
                                     not parsed), so two briefs cannot "agree" by both dropping the same thing
  GC9-snapshot-reload-change-count   the stored copy of the snapshot is the same snapshot
  GC9-wording-difference-count       the two generations really were worded differently, so zero divergences is not
                                     the result of two identical briefs

tests/unit/test_eval_pm23.py perturbs the pipeline (a nondeterministic facts step, a snapshot that changes on
reload, an item that moves bucket, identical wording) and checks each number notices.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from spine.eval.cases import (
    GoldenCase,
    GoldenCaseRegistry,
    MetricResult,
    at_least,
    at_most,
)
from spine.llm.gateway import LLMResponse

from pm.eval.pm12_cases import ScriptedGateway
from pm.eval.pristine import build_pristine_database
from pm.reporting.facts import MorningBriefFacts, compute_morning_brief_facts
from pm.reporting.morning_brief import MorningBrief, generate_morning_brief
from pm.state.snapshot import ProjectSnapshot, build_current_snapshot
from pm.state.store import read_snapshot, save_snapshot

TZ = "Asia/Colombo"
MOMENTS = ("2026-09-15T23:59:59+00:00", "2026-09-18T12:00:00+00:00")  # the morning of the 16th; and the 18th, with its blockers
BUCKETS = ("Committed", "Delivered", "Pending", "Blocked")

Fact = tuple


# --- the fact set, read out of the text a person reads --------------------------------------------------------------------


def facts_from_text(content: str) -> set[Fact]:
    """The facts a reader takes from a rendered brief. Nothing here knows how the brief was made."""
    facts: set[Fact] = set()
    head, *sections = content.split("\n## ")

    if head.startswith("Sprint scope: no sprint"):
        facts.add(("sprint", "none"))
    else:
        sprint = re.search(r"\b(?:sprint-(\d+)|Sprint (\d+))\b", head)
        if sprint:
            facts.add(("sprint", "id", f"sprint-{sprint.group(1) or sprint.group(2)}"))
        for label, pattern in (("day", r"day (\d+) of (\d+)"), ("done", r"(\d+) of (\d+) items")):
            match = re.search(pattern, head, flags=re.IGNORECASE)
            if match:
                facts.add(("sprint", label, int(match.group(1)), int(match.group(2))))

    for section in sections:
        title, _, body = section.partition("\n")
        lines = [line[2:] for line in body.splitlines() if line.startswith("- ")]
        if title == "Blockers":
            facts |= _blocker_facts(lines)
            continue
        if title == "Nobody is assigned":
            for line in lines:
                found = re.match(r"(PM-\d+) \(.*\): nobody is assigned; status (\S+)\.$", line)
                if found:
                    facts.add(("unassigned", found.group(1), found.group(2)))
            continue
        facts.add(("owner", title))
        for line in lines:
            if line.startswith("No update:"):
                facts.add(("no_update", title))
            elif line.startswith("Commits:"):
                facts.add(("commits_only", title, int(re.search(r"(\d+)", line).group(1))))
            else:
                label, _, rest = line.partition(": ")
                if label == "Committed":
                    facts |= {("due", title, due) for due in re.findall(r"\d{4}-\d{2}-\d{2}", rest)}
                elif label in BUCKETS:
                    ids = set(re.findall(r"\bPM-\d+\b", rest))
                    facts.add(("count", title, label, len(ids)))
                    facts |= {("item", title, label, item_id) for item_id in ids}
    return facts


def _blocker_facts(lines: list[str]) -> set[Fact]:
    facts: set[Fact] = set()
    if lines == ["none."]:
        return {("blockers", "none")}
    for rank, line in enumerate(lines, start=1):
        risk = re.search(r"\bRISK-\d+\b", line)
        if not risk:
            continue
        facts.add(("blocker", rank, risk.group(0)))
        severity = re.search(r"\[(low|medium|high)\]", line)
        if severity:
            facts.add(("blocker_severity", risk.group(0), severity.group(1)))
    return facts


# --- the same facts, from the structure the brief was supposed to express ----------------------------------------------------


def facts_from_structure(facts: MorningBriefFacts) -> set[Fact]:
    out: set[Fact] = set()
    sprint = facts.sprint
    if sprint is None:
        out.add(("sprint", "none"))
    else:
        out |= {("sprint", "id", sprint.sprint_id), ("sprint", "day", sprint.day_number, sprint.total_days),
                ("sprint", "done", sprint.done_items, sprint.total_items)}
    out |= {("unassigned", u.item_id, u.status) for u in facts.unassigned}
    for person in facts.people:
        out.add(("owner", person.name))
        if not person.has_activity:
            out.add(("no_update", person.name))
            continue
        if not (person.committed or person.delivered or person.pending or person.blocked):
            out.add(("commits_only", person.name, person.commit_count))
            continue
        out |= {("due", person.name, c.due_date_iso) for c in person.committed if c.due_date_iso}
        for label, bucket in (("Delivered", person.delivered), ("Pending", person.pending), ("Blocked", person.blocked)):
            ids = {item.item_id for item in bucket}
            out.add(("count", person.name, label, len(ids)))
            out |= {("item", person.name, label, item_id) for item_id in ids}
    if not facts.blockers:
        out.add(("blockers", "none"))
    for rank, blocker in enumerate(facts.blockers, start=1):
        out |= {("blocker", rank, blocker.risk_id), ("blocker_severity", blocker.risk_id, blocker.severity)}
    return out


def describe(fact: Fact) -> str:
    return " ".join(str(part) for part in fact)


def divergences(first: set[Fact], second: set[Fact], names: tuple[str, str] = ("generation 1", "generation 2")) -> list[str]:
    """Every fact one has and the other lacks, in words, in a stable order."""
    return ([f"only in {names[0]}: {describe(f)}" for f in sorted(first - second, key=str)]
            + [f"only in {names[1]}: {describe(f)}" for f in sorted(second - first, key=str)])


# --- two models that word everything differently --------------------------------------------------------------------------------


class WordedGateway(ScriptedGateway):
    """The faithful scripted stand-in, with a fixed phrase put in front of every line it writes: the same facts,
    worded differently. ("Today," is on the brief's list of plain connecting words, so every line still grounds.)"""

    def __init__(self, prefix: str = "") -> None:
        super().__init__()
        self._prefix = prefix

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        response = super().generate(prompt, **kwargs)
        if not self._prefix:
            return response
        payload = json.loads(response.text)
        for line in payload["lines"]:
            line["text"] = self._prefix + line["text"]
        return LLMResponse(text=json.dumps(payload), provider="scripted", model="scripted", prompt_tokens=0, completion_tokens=0,
                           latency_ms=0.0, cache_hit=False)


def scripted_pair() -> tuple[WordedGateway, WordedGateway]:
    return WordedGateway(""), WordedGateway("Today, ")


# --- one moment, generated twice ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MomentRun:
    moment: str
    snapshot_changed_on_reload: bool
    first: set[Fact]
    second: set[Fact]
    expected: set[Fact]  # from the structured facts of the first generation
    expected_second: set[Fact]
    lines: int
    lines_worded_differently: int

    @property
    def divergences(self) -> list[str]:
        return divergences(self.first, self.second)

    @property
    def snapshot_gaps(self) -> list[str]:
        return (divergences(self.first, self.expected, ("generation 1", "the snapshot's facts"))
                + divergences(self.second, self.expected_second, ("generation 2", "the snapshot's facts")))


def _line_texts(brief: MorningBrief) -> dict[tuple, str]:
    texts: dict[tuple, str] = {}
    for section, lines in brief.sections.items():
        seen: dict[str, int] = {}
        for line in lines:
            n = seen[line.message_id or ""] = seen.get(line.message_id or "", 0) + 1
            texts[(section, line.message_id, n)] = line.text
    return texts


def run_moment(db: Path, moment: str, gateways: tuple, *, snapshot_for_second: Callable[[ProjectSnapshot], ProjectSnapshot] | None = None) -> MomentRun:
    """Generate the brief twice from one snapshot: the first from the snapshot as built, the second from the stored
    copy of it (the way the job reads it back), each through the whole pipeline (facts, then the model, then grounding)."""
    snapshot = build_current_snapshot(db, taken_at=moment, tz_name=TZ)
    save_snapshot(snapshot, db_path=db)
    stored = read_snapshot(moment, db_path=db)
    if snapshot_for_second is not None:
        stored = snapshot_for_second(stored)

    facts_1 = compute_morning_brief_facts(snapshot)
    brief_1 = generate_morning_brief(facts_1, gateways[0])
    facts_2 = compute_morning_brief_facts(stored)
    brief_2 = generate_morning_brief(facts_2, gateways[1])

    one, two = _line_texts(brief_1), _line_texts(brief_2)
    differing = sum(1 for key in one.keys() | two.keys() if one.get(key) != two.get(key))
    return MomentRun(
        moment=moment, snapshot_changed_on_reload=snapshot.model_dump() != stored.model_dump(),
        first=facts_from_text(brief_1.content), second=facts_from_text(brief_2.content),
        expected=facts_from_structure(facts_1), expected_second=facts_from_structure(facts_2),
        lines=len(one.keys() | two.keys()), lines_worded_differently=differing,
    )


def run_all(directory: Path, gateway_pair: Callable[[], tuple] = scripted_pair, **kwargs) -> list[MomentRun]:
    db = build_pristine_database(directory)
    return [run_moment(db, moment, gateway_pair(), **kwargs) for moment in MOMENTS]


# --- report and metrics ----------------------------------------------------------------------------------------------------------


def format_report(runs: list[MomentRun]) -> str:
    lines = [
        "Golden case 9 -- the facts of the morning brief are deterministic; only the wording may differ",
        "  one snapshot, the brief generated twice (the second time from the stored copy of the snapshot), the two worded differently;",
        "  the fact set is read out of each rendered brief: sprint numbers, owners, each item under its status, counts, due dates, blockers",
    ]
    for run in runs:
        kinds = sorted({fact[0] for fact in run.first | run.second})
        lines.append(
            f"  moment {run.moment[:16]}Z: {len(run.first)} facts vs {len(run.second)} facts "
            f"({', '.join(kinds)}); wording differs on {run.lines_worded_differently} of {run.lines} lines; "
            f"{len(run.divergences)} divergences"
        )
        lines += [f"      {d}" for d in run.divergences[:10]]
        if run.snapshot_gaps:
            lines.append(f"      and {len(run.snapshot_gaps)} differences from the snapshot's own facts, e.g. {run.snapshot_gaps[0]}")
        if run.snapshot_changed_on_reload:
            lines.append("      the stored copy of the snapshot is not the snapshot")
    total = sum(len(run.divergences) for run in runs)
    lines.append(f"  total divergences across the two generations: {total}")
    return "\n".join(lines)


def metrics(runs: list[MomentRun]) -> list[MetricResult]:
    divergent = sum(len(run.divergences) for run in runs)
    gaps = sum(len(run.snapshot_gaps) for run in runs)
    reloads = sum(run.snapshot_changed_on_reload for run in runs)
    worded = sum(run.lines_worded_differently for run in runs)
    facts = sum(len(run.first) for run in runs)
    first = next((d for run in runs for d in run.divergences), None)
    return [
        MetricResult("GC9-fact-divergence-count", "facts that differ between two generations of the brief from the same snapshot (hard zero)",
                     divergent, 0, "at_most", at_most(divergent, 0),
                     f"{facts} facts compared over {len(runs)} moments" + (f"; first: {first}" if first else "")),
        MetricResult("GC9-snapshot-fact-gap-count", "facts in a generation that differ from the facts computed from the snapshot (hard zero)",
                     gaps, 0, "at_most", at_most(gaps, 0), "each generation checked against the structured facts, not only against the other"),
        MetricResult("GC9-snapshot-reload-change-count", "moments where the stored copy of the snapshot is not the snapshot (hard zero)",
                     reloads, 0, "at_most", at_most(reloads, 0), "the second generation is made from the snapshot read back from storage"),
        MetricResult("GC9-wording-difference-count", "lines worded differently between the two generations (at least 1: zero divergence must not be two identical briefs)",
                     worded, 1, "at_least", at_least(worded, 1), f"{worded} lines differ in wording"),
    ]


def measure_gc9() -> list[MetricResult]:
    with tempfile.TemporaryDirectory(prefix="pm_gc9_") as tmp:
        return metrics(run_all(Path(tmp)))


def report() -> str:
    with tempfile.TemporaryDirectory(prefix="pm_gc9_report_") as tmp:
        return format_report(run_all(Path(tmp)))


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC9",
            description="Determinism of facts: the brief generated twice from one snapshot may differ in wording, never in its items, owners, counts or statuses",
            measure_fn=measure_gc9,
        )
    )
