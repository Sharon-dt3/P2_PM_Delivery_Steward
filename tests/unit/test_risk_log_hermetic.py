"""The live risk log is DATA: the delivery lead will edit it (close a risk, add one),
and the repo CSV will change. Tests and golden cases must not depend on it, or a
legitimate edit would break the suite and shift the recorded eval numbers.

So there is a frozen copy of the original three risks for tests and golden cases, and
only real runs (seeding the dev database, the morning job) use the live log.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pm.adapters.risk_log import Risk
from pm.eval.pm12_cases import build_seeded_facts, measure_gc1
from pm.risklog import csv_store
from pm.risklog.csv_store import DEFAULT_CSV_PATH, read_risks, write_risks
from pm.seed.build import RISKS, SEED_RISK_LOG_PATH, build_seed

ORIGINAL = ["RISK-001", "RISK-002", "RISK-003"]


def _edited_live_log(tmp_path):
    """What the live log looks like after the lead closes RISK-001 and adds a risk."""
    risks = read_risks(SEED_RISK_LOG_PATH)
    risks[0] = risks[0].model_copy(update={"status": "mitigated"})
    risks.append(Risk(id="RISK-004", title="Added by the lead", description="d", severity="high", status="open",
                      related_item_id="PM-014", opened_at="2026-09-22"))
    path = tmp_path / "live_edited.csv"
    write_risks(path, risks)
    return path


def test_the_frozen_seed_holds_exactly_the_three_original_risks():
    risks = read_risks(SEED_RISK_LOG_PATH)

    assert [r.id for r in risks] == ORIGINAL
    assert [(r.id, r.severity, r.status, r.related_item_id) for r in risks] == [
        ("RISK-001", "medium", "open", "PM-023"),
        ("RISK-002", "high", "open", "PM-024"),
        ("RISK-003", "low", "mitigated", None),
    ]
    assert [r["id"] for r in RISKS] == ORIGINAL  # the module's constant is the frozen seed


def test_the_test_suite_runs_against_the_frozen_seed_not_the_live_log():
    assert DEFAULT_CSV_PATH == SEED_RISK_LOG_PATH, (
        "the suite must point the risk log at the frozen seed (PM_RISK_LOG_CSV, set in conftest)"
    )


def test_the_live_risk_log_is_a_valid_human_readable_file_whatever_its_current_edits():
    live_path = Path(__file__).resolve().parents[2] / "risk_log" / "risks.csv"

    live = read_risks(live_path)  # strictly validated; never pinned to particular values

    assert live_path.read_text().splitlines()[0] == ",".join(csv_store.COLUMNS)
    assert live and [r.id for r in live] == sorted(r.id for r in live)


def test_seeding_the_dev_database_uses_the_live_log(tmp_path, monkeypatch, seeded_conn):
    """scripts/seed.py (build_seed with no argument) loads the log the repo currently
    holds, so a lead's edit reaches the database the brief reads."""
    monkeypatch.setenv("PM_RISK_LOG_CSV", str(_edited_live_log(tmp_path)))

    build_seed(seeded_conn)

    rows = seeded_conn.execute("SELECT id, status FROM risks ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("RISK-001", "mitigated"), ("RISK-002", "open"), ("RISK-003", "mitigated"), ("RISK-004", "open")]


def test_seeding_with_an_explicit_risk_list_ignores_the_live_log(tmp_path, monkeypatch, seeded_conn):
    monkeypatch.setenv("PM_RISK_LOG_CSV", str(_edited_live_log(tmp_path)))

    build_seed(seeded_conn, risks=RISKS)

    assert [r["id"] for r in seeded_conn.execute("SELECT id FROM risks ORDER BY id").fetchall()] == ORIGINAL


def test_golden_case_numbers_do_not_move_when_the_lead_edits_the_live_log(tmp_path, monkeypatch):
    """GC1 (and GC2, GC3) are built from a pristine database: the recorded numbers are
    reproducible no matter what the live log says today."""
    before = measure_gc1()[0]

    monkeypatch.setenv("PM_RISK_LOG_CSV", str(_edited_live_log(tmp_path)))
    after = measure_gc1()[0]

    assert (after.measured, after.detail) == (before.measured, before.detail) == (0.95, "38 of 40 first-attempt lines resolve")


def test_the_golden_case_facts_contain_the_original_blockers_even_after_a_live_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_RISK_LOG_CSV", str(_edited_live_log(tmp_path)))

    facts = build_seeded_facts()

    assert [b.risk_id for b in facts.blockers] == ["RISK-002", "RISK-001"]  # high first; RISK-001 is still open here


def test_the_dev_database_is_never_read_by_the_golden_cases(monkeypatch, tmp_path):
    """build_seeded_facts() with no database builds its own; it must not open data/pm.db."""
    opened = []
    real_connect = sqlite3.connect

    def spy(database, *a, **kw):
        opened.append(str(database))
        return real_connect(database, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.chdir(tmp_path)  # so a relative data/pm.db would be a different (absent) file

    build_seeded_facts()

    assert not any(p.endswith("data/pm.db") for p in opened)
