"""PM-27 golden case 8: a record whose scope/consent flag is not explicitly true is refused outright, with a logged reason, and
produces zero proposals.

P1 carries a scope flag into every outcome record (`allowlisted`): whether the channel was in scope when the record was made. It is
what stops a channel that was never cleared from leaking into this agent's tracker, risk log and commitments. This case writes one
cleared record (twice: plain, and as a later 1.x version with fields this reader does not know) and eleven kinds of record that must
not be used, every one carrying the same content (a commitment, and a blocker, each naming a real tracker item), so that if a
refused record leaked it would visibly produce something:

    flag false / missing / "true" / "yes" / 1 / null / [true]     the flag is anything but the JSON value true
    flag missing in a file that is also malformed                  the flag is looked at first
    flag true, major version 2.0                                   a contract version this reader does not know
    flag true, an item without its message id                      not the published schema
    a file cut off mid-way                                         not JSON

Every one is read by BOTH things in this agent that consume a record (PM-26's batches and PM-24's commitment feed), twice, each
against a database of its own. The acceptance is the row's own: a record lacking the flag yields zero proposals and one logged
refusal (here: one per consumer per read). Measured from the databases, not from what the code says it did:

  leaked proposals / leaked writes   anything a refused record added to proposals, or to the tracker items, comments, risks or
                                     commitments (hard zero)
  refusal log mismatches             the audit log must hold exactly one `record.refused` row per consumer per read, with the
                                     hand-labelled reason, for every refused record, and none for a cleared one (hard zero)
  wrongly refused / wrongly accepted a cleared record refused by a consumer, or a refused record accepted by one (hard zero)
  non-vacuity                        eleven records really were refused, and the cleared control really produced both batches and
                                     its commitment, so zero leaks is not the result of nothing ever being read

tests/unit/test_eval_pm27.py breaks each guard in turn (a reader as lenient as P1's own pydantic model, which turns "yes" into True;
a refusal that is not logged, or logged under the wrong reason; a reader that refuses everything) and checks the matching number notices.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from spine.eval.cases import (
    GoldenCase,
    GoldenCaseRegistry,
    MetricResult,
    at_least,
    at_most,
)

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.tracker import TrackerMock
from pm.channel.batches import TrackerView, consume
from pm.channel.gate import BATCHES, COMMITMENT_FEED
from pm.channel.record import record_file
from pm.commitments.outcomes import (
    CONSENT_WITHHELD,
    REFUSED,
    MessageInfo,
    ingest_recent_outcomes,
)
from pm.commitments.store import CommitmentTracker
from pm.eval.pristine import build_pristine_database

CHANNEL = "19:proj-gamma@thread.tacv2"
DAY = date(2026, 9, 18)
READS = 2
CONSUMERS = (BATCHES, COMMITMENT_FEED)
MISSING = object()  # the flag is not in the file at all
INFO = {"m1": MessageInfo("mateo.silva", "2026-09-18T09:00:00+00:00")}
ROSTER = {"mateo.silva"}


@dataclass(frozen=True)
class Scenario:
    label: str
    flag: object  # the JSON value written, or MISSING
    expected_code: str | None  # None: a cleared record, which must be used
    version: str = "1.0"
    damage: str | None = None  # None | "bad_item" | "bad_date" | "truncated"
    extras: bool = False  # a later 1.x: fields this reader has never heard of


SCENARIOS = (
    Scenario("control: cleared", True, None),
    Scenario("control: cleared, a later minor version with fields it does not know", True, None, version="1.3", extras=True),
    Scenario("flag false", False, "not_allowlisted"),
    Scenario("flag missing", MISSING, "not_allowlisted"),
    Scenario('flag "true" (a string)', "true", "not_allowlisted"),
    Scenario('flag "yes"', "yes", "not_allowlisted"),
    Scenario("flag 1 (a number)", 1, "not_allowlisted"),
    Scenario("flag null", None, "not_allowlisted"),
    Scenario("flag [true] (a list)", [True], "not_allowlisted"),
    Scenario("flag missing, and the file is malformed too", MISSING, "not_allowlisted", damage="bad_date"),
    Scenario("flag true, major version 2.0", True, "unsupported_version", version="2.0"),
    Scenario("flag true, an item without its message id", True, "schema_invalid", damage="bad_item"),
    Scenario("a file cut off mid-way", True, "not_json", damage="truncated"),
)


def record_text(scenario: Scenario) -> str:
    """The file's text. Every scenario holds the same content, so a leak would show."""
    data = {
        "schema_version": scenario.version, "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": DAY.isoformat(),
        "roster": ["mateo.silva"], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "decisions": [], "questions": [],
        "updates": [{"message_id": "m1", "text": "I'll have PM-016, the reporting dashboard export bug, fixed by Friday.", "quote": "I'll have PM-016"}],
        "blockers": [{"message_id": "m2", "text": "PM-014 is still blocked on the staging migration.", "quote": None}],
    }
    if scenario.flag is not MISSING:
        data["allowlisted"] = scenario.flag
    if scenario.extras:
        data["something_new"] = {"a": 1}
        data["updates"][0]["confidence"] = 0.9
    if scenario.damage == "bad_item":
        del data["blockers"][0]["message_id"]
    if scenario.damage == "bad_date":
        data["date"] = "18 September"
    text = json.dumps(data)
    return text[:120] if scenario.damage == "truncated" else text


@dataclass(frozen=True)
class Observation:
    label: str
    expected_code: str | None
    attempts: int
    proposals_added: int
    writes_added: int  # rows added to the tracker's items, comments, the risk log and the commitments
    refusals: tuple[tuple[str, str], ...]  # (consumer, code) for every record.refused row in the audit log
    accepted_by: frozenset[str]  # the consumers that did not refuse it


# --- the rules, as plain functions of what was observed --------------------------------------------------------------------------


def _refused(observations):
    return [o for o in observations if o.expected_code is not None]


def leaked_proposals(observations) -> int:
    return sum(o.proposals_added for o in _refused(observations))


def leaked_writes(observations) -> int:
    return sum(o.writes_added for o in _refused(observations))


def log_mismatches(observations) -> int:
    """Per record: the refusal rows that should be there and are not, plus the ones that are there and should not be."""
    total = 0
    for o in observations:
        expected = Counter({(c, o.expected_code): o.attempts for c in CONSUMERS}) if o.expected_code else Counter()
        actual = Counter(o.refusals)
        total += sum(((expected - actual) + (actual - expected)).values())
    return total


def wrongly_refused(observations) -> int:
    """A cleared record that a consumer refused, counted per consumer."""
    return sum(len(set(CONSUMERS) - o.accepted_by) for o in observations if o.expected_code is None)


def wrongly_accepted(observations) -> int:
    """A record that must be refused that a consumer took, counted per consumer."""
    return sum(len(o.accepted_by) for o in _refused(observations))


# --- running every record through both consumers ----------------------------------------------------------------------------------------


def _counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("proposals", "items", "item_comments", "risks", "commitments")}
    finally:
        conn.close()


def _refusal_rows(db: Path) -> tuple[tuple[str, str], ...]:
    conn = sqlite3.connect(db)
    try:
        rows = [json.loads(r[0]) for r in conn.execute("SELECT details FROM audit WHERE action = 'record.refused'")]
    finally:
        conn.close()
    return tuple(sorted((r["consumer"], r["code"]) for r in rows))


def observe(directory: Path, scenario: Scenario) -> Observation:
    db = build_pristine_database(directory)
    outcomes = directory / "outcomes"
    path = record_file(outcomes, CHANNEL, DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record_text(scenario), encoding="utf-8")

    view = TrackerView.from_adapters(TrackerMock(db_path=db), RiskLogMock(db_path=db))
    tracker = CommitmentTracker(db)
    before = _counts(db)
    accepted = set(CONSUMERS)
    for _ in range(READS):
        if consume(path, view, db_path=db).refused is not None:
            accepted.discard(BATCHES)
        results = ingest_recent_outcomes(channel_id=CHANNEL, today=DAY, days_back=0, output_dir=outcomes, tracker=tracker,
                                         message_info=INFO.get, roster=ROSTER)
        if any(r.status in (CONSENT_WITHHELD, REFUSED) for r in results):
            accepted.discard(COMMITMENT_FEED)
    after = _counts(db)
    return Observation(
        label=scenario.label, expected_code=scenario.expected_code, attempts=READS,
        proposals_added=after["proposals"] - before["proposals"],
        writes_added=sum(after[t] - before[t] for t in ("items", "item_comments", "risks", "commitments")),
        refusals=_refusal_rows(db), accepted_by=frozenset(accepted),
    )


def run_scenarios(directory: Path) -> list[Observation]:
    out = []
    for index, scenario in enumerate(SCENARIOS):
        folder = directory / f"scenario_{index:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        out.append(observe(folder, scenario))
    return out


# --- report and metrics ---------------------------------------------------------------------------------------------------------------


def format_report(observations: list[Observation]) -> str:
    lines = [
        "Golden case 8 -- a record whose scope/consent flag is not explicitly true is refused, with a logged reason, and produces nothing",
        f"  {len(observations)} records, each carrying the same content (a commitment and a blocker naming real tracker items), each read {READS} times",
        "  by both consumers in this agent (the proposal batches, the commitment feed), each against a database of its own",
    ]
    for o in observations:
        if o.expected_code is None:
            verdict = f"accepted by {', '.join(sorted(o.accepted_by)) or 'neither consumer'}; produced {o.proposals_added} proposals and {o.writes_added} commitment"
        else:
            logged = Counter(code for _, code in o.refusals)
            verdict = (f"refused ({o.expected_code}); proposals {o.proposals_added}, writes {o.writes_added}; "
                       f"logged {sum(logged.values())} refusal row(s) ({', '.join(sorted(logged)) or 'none'}); accepted by {len(o.accepted_by)} consumer(s)")
        lines.append(f"  [{'ok ' if _right(o) else 'BAD'}] {o.label:<66} {verdict}")
    lines += [
        f"  proposals made from records that were refused: {leaked_proposals(observations)}; rows written to the tracker, risk log or commitments: {leaked_writes(observations)}",
        f"  refusal-log rows missing or unexpected: {log_mismatches(observations)}; cleared records refused: {wrongly_refused(observations)}; refused records accepted: {wrongly_accepted(observations)}",
    ]
    return "\n".join(lines)


def _right(o: Observation) -> bool:
    return (not o.proposals_added and not o.writes_added and not o.accepted_by and log_mismatches([o]) == 0) if o.expected_code \
        else (o.accepted_by == set(CONSUMERS) and log_mismatches([o]) == 0 and o.proposals_added > 0)


def metrics(observations: list[Observation]) -> list[MetricResult]:
    proposals, writes, mismatches = leaked_proposals(observations), leaked_writes(observations), log_mismatches(observations)
    wrong_refusals, wrong_accepts = wrongly_refused(observations), wrongly_accepted(observations)
    refused = len(_refused(observations)) if all(_ok_refused(o) for o in _refused(observations)) else 0
    control = sum(o.proposals_added + o.writes_added for o in observations if o.expected_code is None)
    return [
        MetricResult("GC8-leaked-proposal-count", "proposals made from a record that was not cleared or not the contract (hard zero)",
                     proposals, 0, "at_most", at_most(proposals, 0), f"{len(_refused(observations))} refused records, each read {READS} times by both consumers"),
        MetricResult("GC8-leaked-write-count", "rows a refused record added to the tracker's items or comments, the risk log or the commitments (hard zero)",
                     writes, 0, "at_most", at_most(writes, 0), "counted in each scenario's own database"),
        MetricResult("GC8-accepted-refusable-record-count", "refused-kind records that a consumer took anyway (hard zero)",
                     wrong_accepts, 0, "at_most", at_most(wrong_accepts, 0), "per consumer"),
        MetricResult("GC8-refusal-log-mismatch-count", "refusal rows missing, unexpected or under the wrong reason in the audit log: one per consumer per read, none for a cleared record (hard zero)",
                     mismatches, 0, "at_most", at_most(mismatches, 0), "reasons hand-labelled per record"),
        MetricResult("GC8-wrongly-refused-count", "cleared records a consumer refused (hard zero)",
                     wrong_refusals, 0, "at_most", at_most(wrong_refusals, 0), "the plain control and a later 1.x with unknown fields"),
        MetricResult("GC8-refused-record-count", "records of a kind that must be refused, all refused for the labelled reason (at least 11: zero leaks must not be the result of nothing being read)",
                     refused, 11, "at_least", at_least(refused, 11), "flag false, missing, string, yes, number, null, list; flag missing in a malformed file; version 2.0; a bad item; a truncated file"),
        MetricResult("GC8-cleared-control-output-count", "proposals and commitments the two cleared records produced (at least 6: two batches and one commitment each)",
                     control, 6, "at_least", at_least(control, 6), "the same content, with a true flag, is used"),
    ]


def _ok_refused(o: Observation) -> bool:
    return not o.accepted_by and bool(o.refusals) and log_mismatches([o]) == 0


def measure_gc8() -> list[MetricResult]:
    with tempfile.TemporaryDirectory(prefix="pm_gc8_") as tmp:
        return metrics(run_scenarios(Path(tmp)))


def report() -> str:
    with tempfile.TemporaryDirectory(prefix="pm_gc8_report_") as tmp:
        return format_report(run_scenarios(Path(tmp)))


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC8",
            description="Scope and consent refusal: a record whose scope/consent flag is not explicitly true is refused with a logged reason, "
                        "and produces zero proposals, tracker writes or commitments",
            measure_fn=measure_gc8,
        )
    )
