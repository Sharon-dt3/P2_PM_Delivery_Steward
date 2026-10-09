"""PM-30, quantitative reproducibility: every number in the weekly report can be recomputed from the stored snapshots.

Done when: the recomputation test passes for every figure in the report. Three things make that true rather than convenient:

  every week     the check passes for the report of EVERY day the seeded project has data for (47 week-endings, with and without the narrative), and those
                 weeks include the awkward shapes: no sprint, no percentage, an unmapped status, items added after planning
  after the fact a report that was proposed (or approved) can be proven again later from the three stored snapshots it names, and anything that no longer
                 recomputes is named: a changed snapshot, a changed figure, a changed line, a number nobody computed, a snapshot that is gone
  the command    scripts/verify_weekly_report.py does it and says so with its exit code
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from spine.approval.proposals import ProposalStore

from pm.approval import service
from pm.jobs import weekly_report_job as job
from pm.reporting.scripted_weekly import ScriptedWeeklyGateway
from pm.reporting.weekly import compute_weekly_facts, render_weekly_report
from pm.reporting.weekly_check import (
    check_report,
    recompute_figures,
    verify_stored_report,
)
from pm.reporting.weekly_narrative import generate_narrative
from pm.state.snapshot import build_current_snapshot

TZ = "Asia/Colombo"
FRIDAY = datetime(2026, 9, 18, 12, 30, tzinfo=timezone.utc)
FIRST, LAST = date(2026, 8, 24), date(2026, 10, 9)  # Sprint 12's first day to the day this was written: every day the project has history for
POLICY = service.ApprovalPolicy(approver_ids=frozenset({"sharon"}))


def snaps(db, day: date):
    moment = datetime(day.year, day.month, day.day, 12, 30, tzinfo=timezone.utc)
    return tuple(build_current_snapshot(db, taken_at=(moment - n * job.WEEK).isoformat(), tz_name=TZ) for n in (0, 1, 2))


# --- every week -------------------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def sweep(seeded_db_path):
    """The report, with and without the narrative, for every day the seeded project has data for (a throwaway database and a frozen risk log)."""
    rows = []
    day = FIRST
    while day <= LAST:
        end, start, before = snaps(seeded_db_path, day)
        facts = compute_weekly_facts(end, start, before)
        plain = render_weekly_report(facts)
        narrated = render_weekly_report(facts, generate_narrative(facts, ScriptedWeeklyGateway()))
        rows.append((day, facts, plain, narrated, (end, start, before)))
        day += timedelta(days=1)
    return rows


def test_every_figure_recomputes_for_the_report_of_every_day_with_and_without_the_narrative(sweep):
    assert len(sweep) == (LAST - FIRST).days + 1 == 47

    failures = {}
    for day, facts, plain, narrated, (end, start, previous) in sweep:
        for label, text in (("quantities", plain), ("with narrative", narrated)):
            problems = check_report(facts, text, end, start, previous)
            if problems:
                failures[f"{day} {label}"] = problems

    assert failures == {}


def test_the_weeks_swept_include_the_awkward_shapes_so_passing_them_all_means_something(sweep):
    facts = [f for _, f, *_ in sweep]

    assert any(f.sprint is None for f in facts)  # a date no sprint covers
    assert any(f.sprint is not None for f in facts)
    assert any(f.velocity_change_percent is None for f in facts)  # a week after a week of nothing completed
    assert any(f.velocity_change_percent is not None for f in facts)
    assert any(f.unmapped for f in facts)  # a status the tracker does not map
    assert any(f.added_after_planning for f in facts)  # the two items added mid-sprint
    assert any(f.blocked for f in facts) and any(not f.blocked for f in facts)
    assert len({tuple(sorted(k.key for k in f.figures)) for f in facts}) >= 4  # the reports state different sets of figures, not one shape 47 times


def test_the_recomputation_covers_every_figure_a_report_states_and_no_figure_goes_unstated(sweep):
    for day, facts, _plain, _narrated, (end, start, previous) in sweep:
        stated = {f.key: f.value for f in facts.figures}
        assert stated == recompute_figures(end, start, previous), day  # the same keys and the same values, no more and no fewer


# --- after the fact -----------------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def report(seeded_db_path):
    return job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, gateway=ScriptedWeeklyGateway())


def test_a_report_that_was_proposed_proves_again_from_its_stored_snapshots(seeded_db_path, report):
    assert verify_stored_report(report.proposal_id, db_path=seeded_db_path) == []


def test_an_approved_report_still_proves_and_so_does_one_without_a_narrative(seeded_db_path, report):
    service.approve_and_send(report.proposal_id, approver_id="sharon", policy=POLICY, db_path=seeded_db_path)
    bare = job.run_weekly_report_job(FRIDAY + timedelta(days=7), db_path=seeded_db_path, timezone_name=TZ, gateway=None)

    assert verify_stored_report(report.proposal_id, db_path=seeded_db_path) == []
    assert verify_stored_report(bare.proposal_id, db_path=seeded_db_path) == []


def _edit_proposal(db, proposal_id, change):
    conn = sqlite3.connect(db)
    payload = json.loads(conn.execute("SELECT payload FROM proposals WHERE id = ?", (proposal_id,)).fetchone()[0])
    change(payload)
    conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(payload), proposal_id))
    conn.commit()
    conn.close()


def test_a_figure_changed_after_the_report_was_made_is_named(seeded_db_path, report):
    def tamper(payload):
        for figure in payload["figures"]:
            if figure["key"] == "completed_this_week":
                figure["value"] += 1

    _edit_proposal(seeded_db_path, report.proposal_id, tamper)

    assert any("completed_this_week: the report stated 4 but the snapshots give 3" in p for p in verify_stored_report(report.proposal_id, db_path=seeded_db_path))


def test_a_line_changed_in_the_text_is_named(seeded_db_path, report):
    _edit_proposal(seeded_db_path, report.proposal_id, lambda p: p.update(content=p["content"].replace("up 50%", "up 80%")))

    problems = verify_stored_report(report.proposal_id, db_path=seeded_db_path)

    assert any("the report no longer matches its snapshots" in p and "up 50%" in p for p in problems)
    assert any("the text states 80" in p for p in problems)


def test_a_number_nobody_computed_added_to_the_text_is_named(seeded_db_path, report):
    _edit_proposal(seeded_db_path, report.proposal_id, lambda p: p.update(content=p["content"] + "\nIn short: completed items are up 99%."))

    assert any("the text states 99" in p for p in verify_stored_report(report.proposal_id, db_path=seeded_db_path))


def test_a_stored_snapshot_that_changed_since_is_noticed_because_the_report_no_longer_recomputes(seeded_db_path, report):
    named = ProposalStore(seeded_db_path).get(report.proposal_id).payload["snapshots"]["end"]
    conn = sqlite3.connect(seeded_db_path)
    payload = json.loads(conn.execute("SELECT payload FROM snapshots WHERE taken_at = ?", (named,)).fetchone()[0])
    payload["items"] = [i for i in payload["items"] if i["id"] != "PM-016"]  # an item quietly removed from the stored copy
    conn.execute("UPDATE snapshots SET payload = ? WHERE taken_at = ?", (json.dumps(payload), named))
    conn.commit()
    conn.close()

    assert verify_stored_report(report.proposal_id, db_path=seeded_db_path) != []


def test_a_snapshot_that_is_no_longer_stored_means_it_cannot_be_proven_and_says_which(seeded_db_path, report):
    named = ProposalStore(seeded_db_path).get(report.proposal_id).payload["snapshots"]["start"]
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("DELETE FROM snapshots WHERE taken_at = ?", (named,))
    conn.commit()
    conn.close()

    problems = verify_stored_report(report.proposal_id, db_path=seeded_db_path)

    assert len(problems) == 1 and f"the start snapshot ({named!r}) is not stored" in problems[0]


def test_an_unknown_proposal_and_one_that_is_not_a_weekly_report_are_said_so(seeded_db_path):
    other = ProposalStore(seeded_db_path).create(type="morning_brief_publish", payload={}, original_model_output={}, source_refs=[], idempotency_key="x")

    assert verify_stored_report("nope", db_path=seeded_db_path) == ["there is no proposal 'nope'"]
    assert "not a weekly report" in verify_stored_report(other.id, db_path=seeded_db_path)[0]


# --- the command -------------------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_weekly_report.py"
    spec = importlib.util.spec_from_file_location("verify_weekly_report_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_command_says_every_figure_recomputes_and_exits_zero(script, seeded_db_path, report, capsys):
    status = script.main(["--latest", "--db", str(seeded_db_path)])

    assert status == 0 and "every figure recomputes from its stored snapshots" in capsys.readouterr().out
    assert script.main(["--proposal", report.proposal_id[:8], "--db", str(seeded_db_path)]) == 0


def test_the_command_exits_one_and_lists_what_does_not_recompute(script, seeded_db_path, report, capsys):
    _edit_proposal(seeded_db_path, report.proposal_id, lambda p: p.update(content=p["content"] + "\nIn short: up 99%."))

    status = script.main(["--latest", "--db", str(seeded_db_path)])

    out = capsys.readouterr().out
    assert status == 1 and "problem(s)" in out and "the text states 99" in out


def test_the_command_says_so_when_there_is_no_such_report(script, seeded_db_path, capsys):
    assert script.main(["--latest", "--db", str(seeded_db_path)]) == 1
    assert "no such weekly report" in capsys.readouterr().out
