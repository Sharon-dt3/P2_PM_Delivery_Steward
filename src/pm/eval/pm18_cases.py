"""PM-18 golden case 4: gap precision and no duplicate.

Two things are graded, both against labels written by hand, never taken from the code
under test:

1. Precision and recall of the GAP SET (pm.risk.gaps.find_gaps): the current blockers
   that have no risk-log entry. Pooled over scenarios whose right answer is known:
   the seeded project at three moments (read off the seed's own item histories and
   risk log) and a hand-built project full of decoys -- blockers covered by an open,
   a mitigated and a closed risk; an item that is merely in progress with an open risk;
   a free-text status that is not "blocked"; a risk that names no item; an unassigned
   blocker that IS a gap. A detector that is too eager lowers precision, one that
   misses lowers recall; both must be 1.0.

2. The reject-and-rerun assertion: propose, reject one proposal, rerun (three times),
   and no duplicate may appear (GC4-duplicate-count, a hard zero), read from the raw
   database rows. The flip side is checked too, so "never propose anything" cannot
   pass: after a MATERIAL change to the rejected blocker a new proposal must appear,
   once, stating the change (GC4-missed-reproposal-count, a hard zero).

tests/unit/test_eval_pm18.py breaks the detector and the memory in turn and checks that
each number notices.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from spine.eval.cases import (
    GoldenCase,
    GoldenCaseRegistry,
    MetricResult,
    at_least,
    at_most,
)

from pm.adapters.risk_log import Risk
from pm.adapters.tracker import Assignee, Sprint
from pm.approval import service
from pm.approval.service import ApprovalPolicy
from pm.eval.pristine import build_pristine_database
from pm.risk import gaps as gap_module
from pm.risk import proposals as proposal_module
from pm.risk.scripted import ScriptedRiskGateway
from pm.state.snapshot import (
    ChannelSnapshot,
    NormalizedItem,
    ProjectSnapshot,
    build_current_snapshot,
)

APPROVER = "gc4.approver"
POLICY = ApprovalPolicy(approver_ids=frozenset({APPROVER}))
TZ = "Asia/Colombo"
AS_OF = "2026-09-18T12:00:00+00:00"


@dataclass(frozen=True)
class Scenario:
    name: str
    expected: frozenset[str]  # the hand-labelled gap set
    why: str


# --- the seeded project, labelled by reading pm/seed/build.py ------------------------------------------------
# Blocked items and when they became blocked: PM-023 10 Sep, PM-024 11 Sep, PM-014 14 Sep, PM-015 17 Sep.
# PM-022 is "waiting_on_vendor" (not canonical, never a blocker). The risk log: RISK-001 -> PM-023,
# RISK-002 -> PM-024, RISK-003 -> no item.
SEEDED = [
    ("2026-09-18T12:00:00+00:00", Scenario(
        "seeded, 18 Sep", frozenset({"PM-014", "PM-015"}),
        "four blockers; PM-023 and PM-024 are in the log, PM-014 and PM-015 are not")),
    ("2026-09-16T12:00:00+00:00", Scenario(
        "seeded, 16 Sep", frozenset({"PM-014"}),
        "PM-015 only becomes blocked on 17 Sep, so it is not yet a blocker")),
    ("2026-09-12T12:00:00+00:00", Scenario(
        "seeded, 12 Sep", frozenset(),
        "only PM-023 and PM-024 are blocked, and both are in the log")),
]


def _item(item_id: str, status: str, *, assignee: str | None = "aisha.rahman", blocked_since: str | None = "2026-09-14") -> NormalizedItem:
    return NormalizedItem(
        id=item_id, title=f"Work item {item_id}", status=status, raw_status=status, sprint_id="sprint-13", assignee_id=assignee,
        created_at="2026-09-10T00:00:00+00:00", blocked_since=blocked_since if status == "blocked" else None,
    )


def _risk(risk_id: str, item: str | None, status: str) -> Risk:
    return Risk(id=risk_id, title="t", description="d", severity="low", status=status, related_item_id=item, opened_at="2026-09-10")


def _decoy_project() -> tuple[ProjectSnapshot, Scenario]:
    items = [
        _item("PM-101", "blocked"),  # covered by an OPEN risk
        _item("PM-102", "blocked"),  # covered by a MITIGATED risk
        _item("PM-103", "blocked"),  # covered by a CLOSED risk
        _item("PM-104", "blocked"),  # a gap
        _item("PM-105", "blocked", assignee=None),  # a gap nobody owns
        _item("PM-106", "in_progress"),  # not blocked, though a risk names it
        _item("PM-107", "done"),
        _item("PM-108", "waiting_on_vendor"),  # free text outside the canonical set: not "blocked"
        _item("PM-109", "backlog"),
    ]
    risks = [
        _risk("RISK-101", "PM-101", "open"), _risk("RISK-102", "PM-102", "mitigated"), _risk("RISK-103", "PM-103", "closed"),
        _risk("RISK-106", "PM-106", "open"), _risk("RISK-199", None, "open"),  # names no item: covers nothing
    ]
    snapshot = ProjectSnapshot(
        taken_at=AS_OF, items=items, commits=[], channel=ChannelSnapshot(channel_id="c", messages=[]),
        sprints=[Sprint(id="sprint-13", display_name="Sprint 13", start_date="2026-09-07", end_date="2026-09-20")],
        risks=risks, roster=[Assignee(id="aisha.rahman", display_name="Aisha Rahman")], timezone="UTC",
    )
    return snapshot, Scenario(
        "decoys", frozenset({"PM-104", "PM-105"}),
        "any-status risks cover; an in-progress or free-text item is no blocker; a risk naming no item covers nothing",
    )


def _no_blockers() -> tuple[ProjectSnapshot, Scenario]:
    snapshot, _ = _decoy_project()
    return snapshot.model_copy(update={"items": [i for i in snapshot.items if i.status != "blocked"]}), Scenario(
        "no blockers", frozenset(), "nothing is blocked, so nothing can be a gap")


# --- precision and recall ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scored:
    scenario: Scenario
    found: frozenset[str]

    @property
    def true_positives(self) -> frozenset[str]:
        return self.found & self.scenario.expected

    @property
    def false_positives(self) -> frozenset[str]:
        return self.found - self.scenario.expected

    @property
    def false_negatives(self) -> frozenset[str]:
        return self.scenario.expected - self.found


def score_gap_set(directory: Path) -> list[Scored]:
    """Run the real gap detector over every scenario and pair what it found with the hand label."""
    db = build_pristine_database(directory)
    scored = []
    for moment, scenario in SEEDED:
        snapshot = build_current_snapshot(db, taken_at=moment, tz_name=TZ)
        scored.append(Scored(scenario, frozenset(g.item_id for g in gap_module.find_gaps(snapshot))))
    for snapshot, scenario in (_decoy_project(), _no_blockers()):
        scored.append(Scored(scenario, frozenset(g.item_id for g in gap_module.find_gaps(snapshot))))
    return scored


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def precision_recall(scored: list[Scored]) -> tuple[float, float]:
    tp = sum(len(s.true_positives) for s in scored)
    fp = sum(len(s.false_positives) for s in scored)
    fn = sum(len(s.false_negatives) for s in scored)
    return _ratio(tp, tp + fp), _ratio(tp, tp + fn)


def format_gap_report(scored: list[Scored]) -> str:
    precision, recall = precision_recall(scored)
    lines = ["Golden case 4 -- risk-log gap set", "  hand-labelled gap set vs what find_gaps() reported:"]
    for s in scored:
        mark = "ok " if not s.false_positives and not s.false_negatives else "BAD"
        lines.append(
            f"  [{mark}] {s.scenario.name:<16} expected {sorted(s.scenario.expected) or '[]'}  found {sorted(s.found) or '[]'}"
            + (f"  false positives {sorted(s.false_positives)}" if s.false_positives else "")
            + (f"  false negatives {sorted(s.false_negatives)}" if s.false_negatives else "")
        )
    lines.append(f"  precision {precision:.2f}   recall {recall:.2f}")
    return "\n".join(lines)


# --- reject, rerun, no duplicate ----------------------------------------------------------------------------------


def _rows(db: Path) -> list[tuple[str, str, dict]]:
    """(id, status, payload) of every risk proposal, straight from the database."""
    conn = sqlite3.connect(db)
    try:
        return [(i, s, json.loads(p)) for i, s, p in conn.execute(
            "SELECT id, status, payload FROM proposals WHERE type = 'risk_log_entry' ORDER BY created_at, id")]
    finally:
        conn.close()


def _propose(db: Path, snapshot: ProjectSnapshot) -> None:
    proposal_module.detect_and_propose(
        snapshot, ScriptedRiskGateway(gap_module.find_gaps(snapshot)), db_path=db)


@dataclass(frozen=True)
class RerunProbe:
    duplicates: int
    missed_reproposals: int
    detail_duplicates: str
    detail_reproposal: str


def probe_reject_and_rerun(directory: Path) -> RerunProbe:
    db = build_pristine_database(directory)
    snapshot = build_current_snapshot(db, taken_at=AS_OF, tz_name=TZ)

    _propose(db, snapshot)
    first = _rows(db)
    problems_first = len(first) - len({p["item_id"] for _, _, p in first})  # two proposals for one blocker
    target = next((i for i, _, p in first if p["item_id"] == "PM-014"), None)
    if target is None or len(first) != 2:  # the probe's own setup is wrong: say so rather than pass vacuously
        return RerunProbe(1, 1, f"setup failed: {len(first)} proposals after the first run", "setup failed")

    service.reject(target, approver_id=APPROVER, reason="gc4", policy=POLICY, db_path=db)
    rejected_now = {i: s for i, s, _ in _rows(db)}
    if rejected_now.get(target) != "rejected":
        return RerunProbe(1, 1, "setup failed: the rejection did not land", "setup failed")

    for _ in range(3):
        _propose(db, snapshot)
    after = _rows(db)
    extra = [i for i, _, _ in after if i not in {j for j, _, _ in first}]
    duplicates = problems_first + len(extra)
    statuses = ", ".join(f"{p['item_id']} {s}" for _, s, p in after)

    # the flip side: a material change to the rejected blocker must produce one new proposal, stating the change
    moved = snapshot.model_copy(update={"items": [
        i.model_copy(update={"blocked_since": "2026-09-18"}) if i.id == "PM-014" else i for i in snapshot.items]})
    before_ids = {i for i, _, _ in after}
    _propose(db, moved)
    _propose(db, moved)  # and the change is itself proposed only once
    new = [(i, s, p) for i, s, p in _rows(db) if i not in before_ids]
    missed = 0
    if len(new) != 1:
        missed += 1
    else:
        _, status, payload = new[0]
        change = payload.get("change") or {}
        fields = {d.get("field") for d in change.get("differences", [])}
        if not (payload["item_id"] == "PM-014" and status == "pending" and change.get("previous_proposal_id") == target
                and "blocked_since" in fields and "was 2026-09-14" in change.get("text", "")):
            missed += 1
    return RerunProbe(
        duplicates, missed,
        f"{len(first)} proposed, PM-014 rejected, rerun x3: {len(after)} proposals ({statuses}); {len(extra)} new",
        f"after PM-014 was re-blocked on 2026-09-18: {len(new)} new proposal(s)"
        + (f", change stated: {new[0][2].get('change', {}).get('text', 'none')[:90]}" if len(new) == 1 and new[0][2].get("change") else ""),
    )


# --- the case ---------------------------------------------------------------------------------------------------------


def measure_gc4() -> list[MetricResult]:
    with tempfile.TemporaryDirectory(prefix="pm_gc4_") as tmp:
        scored = score_gap_set(Path(tmp) / "gaps")
        probe = probe_reject_and_rerun(Path(tmp) / "rerun")
    precision, recall = precision_recall(scored)
    tp = sum(len(s.true_positives) for s in scored)
    fp = sum(len(s.false_positives) for s in scored)
    fn = sum(len(s.false_negatives) for s in scored)
    detail = f"{tp} true positives, {fp} false positives, {fn} false negatives over {len(scored)} scenarios"
    bad = [s for s in scored if s.false_positives or s.false_negatives]
    if bad:
        detail += f"; first miss: {bad[0].scenario.name} (false positives {sorted(bad[0].false_positives)}, false negatives {sorted(bad[0].false_negatives)})"
    return [
        MetricResult("GC4-gap-precision", "gaps reported that are really missing from the risk log", precision, 1.0, "at_least",
                     at_least(precision, 1.0), detail),
        MetricResult("GC4-gap-recall", "real gaps the detector reported", recall, 1.0, "at_least", at_least(recall, 1.0), detail),
        MetricResult("GC4-duplicate-count", "duplicate proposals after a rejection and three reruns (hard zero)", probe.duplicates, 0,
                     "at_most", at_most(probe.duplicates, 0), probe.detail_duplicates),
        MetricResult("GC4-missed-reproposal-count", "material changes to a rejected blocker not proposed again with the change stated (hard zero)",
                     probe.missed_reproposals, 0, "at_most", at_most(probe.missed_reproposals, 0), probe.detail_reproposal),
    ]


def report() -> str:
    """The hand-label comparison, printed by scripts/run_eval.py."""
    with tempfile.TemporaryDirectory(prefix="pm_gc4_report_") as tmp:
        return format_gap_report(score_gap_set(Path(tmp)))


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC4",
            description="Risk-log gap detection: precision and recall of the gap set; reject, rerun, no duplicate",
            measure_fn=measure_gc4,
        )
    )
