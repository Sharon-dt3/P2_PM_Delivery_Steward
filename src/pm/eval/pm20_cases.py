"""PM-20 golden case 5: promotion threshold reconfiguration.

The promotion threshold (PM-19) must be configuration, not a literal. This case proves it by
running the same project, with the same code, at a two-day threshold and then at four days, where
the ONLY thing that changes between the runs is the number in the configuration file, edited in
place. It then asserts the proposed set shrinks exactly as it should.

The project is the seeded one plus seven hand-built blockers, so the blockers that are NOT in the
risk log have known ages (days blocked on 18 Sep, read off the transitions written below):

    PM-101 0    PM-102 1    PM-015 1    PM-103 2    PM-104 3    PM-014 4    PM-105 4    PM-106 5    PM-107 10

(PM-023 and PM-024 are older still but already in the risk log, so are never proposed.) A blocker
is proposed when it is OLDER than the threshold, so by hand:

    threshold 2 -> PM-104, PM-014, PM-105, PM-106, PM-107
    threshold 4 -> PM-106, PM-107
    dropped when the threshold goes from 2 to 4 -> PM-104, PM-014, PM-105 (the ones aged 3 and 4); nothing is added

Each run goes through the two production entry points that read the configuration: the morning
job and scripts/detect_risks.py. The proposed set is read straight from the database rows. Four
numbers, each a hard zero: the wrong blockers at two days, the wrong blockers at four days, a
shrink that is not exactly the expected one (including anything newly proposed), and the two
entry points disagreeing.

tests/unit/test_eval_pm20.py breaks the code in turn (a literal threshold, a threshold read once
and remembered, an off-by-one, a proposer that ignores the policy) and checks each number notices.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, time, timezone
from pathlib import Path

from spine.eval.cases import GoldenCase, GoldenCaseRegistry, MetricResult, at_most

from pm.approval.service import ApprovalPolicy
from pm.eval.pm12_cases import ScriptedGateway
from pm.eval.pristine import build_pristine_database
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.risk.gaps import find_gaps
from pm.risk.scripted import ScriptedRiskGateway
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import build_current_snapshot

REPO_ROOT = Path(__file__).resolve().parents[3]
TZ = "Asia/Colombo"
JOB_MOMENT = datetime(2026, 9, 18, 2, 30, tzinfo=timezone.utc)  # Fri 08:00 in Colombo
CLI_MOMENT = "2026-09-18T12:00"  # the same local date

# (item, the date its status moved to blocked). The age on 18 Sep is given beside each.
HAND_BUILT_BLOCKERS = [
    ("PM-101", "2026-09-18"),  # 0 days
    ("PM-102", "2026-09-17"),  # 1
    ("PM-103", "2026-09-16"),  # 2
    ("PM-104", "2026-09-15"),  # 3
    ("PM-105", "2026-09-14"),  # 4
    ("PM-106", "2026-09-13"),  # 5
    ("PM-107", "2026-09-08"),  # 10
]
AGES = {"PM-101": 0, "PM-102": 1, "PM-015": 1, "PM-103": 2, "PM-104": 3, "PM-014": 4, "PM-105": 4, "PM-106": 5, "PM-107": 10}

# The hand labels. Written out, not computed: they are what the code is graded against.
EXPECTED_AT_TWO = frozenset({"PM-104", "PM-014", "PM-105", "PM-106", "PM-107"})
EXPECTED_AT_FOUR = frozenset({"PM-106", "PM-107"})
EXPECTED_DROPPED = frozenset({"PM-104", "PM-014", "PM-105"})
EXPECTED_ADDED: frozenset[str] = frozenset()

LOW, HIGH = 2, 4  # the two configuration values the case writes into the file


@contextlib.contextmanager
def _environment(**values: str) -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _fresh_world(directory: Path) -> Path:
    db = build_pristine_database(directory)
    conn = sqlite3.connect(db)
    try:
        for item_id, entered_on in HAND_BUILT_BLOCKERS:
            conn.execute(
                "INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id) "
                "VALUES (?, ?, 'blocked', 'sprint-13', 'olivia.dupree', '2026-09-01', ?, NULL)",
                (item_id, f"Hand-built blocker {item_id}", entered_on),
            )
            for frm, to, at in (("backlog", "in_progress", "2026-09-02"), ("in_progress", "blocked", entered_on)):
                conn.execute("INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?)",
                             (item_id, frm, to, at))
        conn.commit()
    finally:
        conn.close()
    return db


def proposed_items(db: Path) -> frozenset[str]:
    """The items a risk proposal exists for, read straight from the database rows."""
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT payload FROM proposals WHERE type = 'risk_log_entry'").fetchall()
    finally:
        conn.close()
    return frozenset(json.loads(payload)["item_id"] for (payload,) in rows)


class _JobGateway:
    """One scripted stand-in serving both the brief and the risk prose, so the job runs end to end."""

    def __init__(self, db: Path) -> None:
        snapshot = build_current_snapshot(db, taken_at=JOB_MOMENT.isoformat(), tz_name=TZ)
        self.brief, self.risk = ScriptedGateway(), ScriptedRiskGateway(find_gaps(snapshot))

    def generate(self, prompt: str, **kwargs: object):
        risk = "reference_id: item:" in prompt and "risk log entry" in prompt.lower()
        return (self.risk if risk else self.brief).generate(prompt, **kwargs)


def run_through_the_job(db: Path, config_file: Path) -> frozenset[str]:
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone=TZ, working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    with _environment(PM_RISK_DETECTION="1", PM_RISK_PROMOTION_CONFIG=str(config_file), PM_RISK_PROMOTION_THRESHOLD_DAYS="",
                      PM_RISK_LOG_SYNC="0", PM_SUPABASE_MIRROR="0"):
        run_morning_brief_job(config, _JobGateway(db), moment=JOB_MOMENT, db_path=db, policy=ApprovalPolicy())
    return proposed_items(db)


def run_through_the_command(db: Path, config_file: Path) -> frozenset[str]:
    """scripts/detect_risks.py, loaded fresh each time, pointed at the configuration file."""
    spec = importlib.util.spec_from_file_location("detect_risks_gc5", REPO_ROOT / "scripts" / "detect_risks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with _environment(PM_RISK_LOG_SYNC="0", PM_SUPABASE_MIRROR="0"), contextlib.redirect_stdout(io.StringIO()):
        module.main(["--db", str(db), "--at", CLI_MOMENT, "--gateway", "scripted", "--promotion-config", str(config_file)])
    return proposed_items(db)


DRIVERS = {"the morning job": run_through_the_job, "detect_risks.py": run_through_the_command}


def run_both_thresholds(directory: Path) -> dict[str, dict[int, frozenset[str]]]:
    """For each entry point: one configuration file, edited in place from LOW to HIGH between two
    runs, each run on a fresh copy of the project. Returns {entry point: {threshold: proposed items}}."""
    results: dict[str, dict[int, frozenset[str]]] = {}
    for name, drive in DRIVERS.items():
        config_file = directory / f"{name.split('.')[0].replace(' ', '_')}_risk_promotion.yaml"
        results[name] = {}
        for threshold in (LOW, HIGH):
            config_file.write_text(f"threshold_days: {threshold}\n")  # the only thing that changes
            db = _fresh_world(directory / f"{name.split('.')[0].replace(' ', '_')}_{threshold}")
            results[name][threshold] = drive(db, config_file)
    return results


# --- grading ----------------------------------------------------------------------------------------------------


def _errors(found: frozenset[str], expected: frozenset[str]) -> int:
    return len(found - expected) + len(expected - found)


def grade(results: dict[str, dict[int, frozenset[str]]]) -> dict[str, int]:
    job = results["the morning job"]
    two_errors = sum(_errors(runs[LOW], EXPECTED_AT_TWO) for runs in results.values())
    four_errors = sum(_errors(runs[HIGH], EXPECTED_AT_FOUR) for runs in results.values())
    shrink_errors = 0
    for runs in results.values():
        dropped, added = runs[LOW] - runs[HIGH], runs[HIGH] - runs[LOW]
        shrink_errors += _errors(dropped, EXPECTED_DROPPED) + _errors(added, EXPECTED_ADDED)
    disagreement = sum(len(job[t] ^ results["detect_risks.py"][t]) for t in (LOW, HIGH))
    return {"two": two_errors, "four": four_errors, "shrink": shrink_errors, "disagreement": disagreement}


def _listing(items: frozenset[str]) -> str:
    return "[" + ", ".join(sorted(items, key=lambda i: (AGES.get(i, 99), i))) + "]"


def format_report(results: dict[str, dict[int, frozenset[str]]]) -> str:
    lines = [
        "Golden case 5 -- promotion threshold reconfiguration",
        "  one project, one codebase; only threshold_days in the configuration file is edited between the runs",
        "  blockers missing from the risk log, by hand-labelled age (days): "
        + ", ".join(f"{i} {a}" for i, a in sorted(AGES.items(), key=lambda kv: (kv[1], kv[0]))),
    ]
    for name, runs in results.items():
        lines.append(f"  via {name}:")
        for threshold, expected in ((LOW, EXPECTED_AT_TWO), (HIGH, EXPECTED_AT_FOUR)):
            found = runs[threshold]
            mark = "ok " if found == expected else "BAD"
            lines.append(f"    [{mark}] threshold {threshold} days  proposed {_listing(found)}"
                         f"  expected {_listing(expected)}")
        dropped, added = runs[LOW] - runs[HIGH], runs[HIGH] - runs[LOW]
        mark = "ok " if dropped == EXPECTED_DROPPED and added == EXPECTED_ADDED else "BAD"
        lines.append(f"    [{mark}] going from {LOW} to {HIGH} days  dropped {_listing(dropped)} (expected {_listing(EXPECTED_DROPPED)})"
                     f"  newly proposed {_listing(added)} (expected none)")
    return "\n".join(lines)


def measure_gc5() -> list[MetricResult]:
    with tempfile.TemporaryDirectory(prefix="pm_gc5_") as tmp:
        results = run_both_thresholds(Path(tmp))
    errors = grade(results)
    job = results["the morning job"]
    return [
        MetricResult("GC5-two-day-set-errors", "blockers wrongly proposed or missed at a two-day threshold (hard zero)", errors["two"], 0,
                     "at_most", at_most(errors["two"], 0), f"proposed {_listing(job[LOW])}; expected {_listing(EXPECTED_AT_TWO)}"),
        MetricResult("GC5-four-day-set-errors", "blockers wrongly proposed or missed at a four-day threshold (hard zero)", errors["four"], 0,
                     "at_most", at_most(errors["four"], 0), f"proposed {_listing(job[HIGH])}; expected {_listing(EXPECTED_AT_FOUR)}"),
        MetricResult("GC5-shrink-errors", "blockers dropped or newly proposed when the threshold goes from two to four days, "
                     "other than the expected ones (hard zero)", errors["shrink"], 0, "at_most", at_most(errors["shrink"], 0),
                     f"dropped {_listing(job[LOW] - job[HIGH])}; expected {_listing(EXPECTED_DROPPED)}; "
                     f"newly proposed {_listing(job[HIGH] - job[LOW])}"),
        MetricResult("GC5-entry-point-disagreement-count", "blockers the morning job and detect_risks.py propose differently (hard zero)",
                     errors["disagreement"], 0, "at_most", at_most(errors["disagreement"], 0),
                     "both entry points read the same configuration file at each run"),
    ]


def report() -> str:
    """The two runs and the set differences, printed by scripts/run_eval.py."""
    with tempfile.TemporaryDirectory(prefix="pm_gc5_report_") as tmp:
        return format_report(run_both_thresholds(Path(tmp)))


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC5",
            description="Promotion threshold reconfiguration: at two days then four, the proposed set shrinks exactly as expected",
            measure_fn=measure_gc5,
        )
    )
