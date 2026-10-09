"""PM-29 and PM-30: the weekly status report, and the check that every number in it recomputes from the stored snapshots.

Done when (PM-29): the scope-change section correctly identifies the two items added mid-sprint.
Done when (PM-30): the recomputation test passes for every figure in the report.

The report is a fixed template over three snapshots a week apart. Nothing is worded by a model, so the same snapshots make the same report; the agent
never sends it, so its proposal can be read and rejected and nothing else.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from spine.approval.proposals import ProposalStore

from pm.approval import service
from pm.approval.proposals import WEEKLY_REPORT_PROPOSAL_TYPE
from pm.jobs import weekly_report_job as job
from pm.reporting.weekly import compute_weekly_facts, render_weekly_report
from pm.reporting.weekly_check import (
    check_report,
    recompute_figures,
    unexplained_numbers,
)
from pm.state.snapshot import build_current_snapshot
from pm.state.store import read_snapshot

TZ = "Asia/Colombo"
FRIDAY = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)  # day 12 of Sprint 13: both planted mid-sprint items exist by now
WEEK = job.WEEK


def snapshots(db, end=FRIDAY):
    return tuple(build_current_snapshot(db, taken_at=(end - n * WEEK).isoformat(), tz_name=TZ) for n in (0, 1, 2))


@pytest.fixture()
def report(seeded_db_path):
    end, start, previous = snapshots(seeded_db_path)
    facts = compute_weekly_facts(end, start, previous)
    return facts, render_weekly_report(facts), (end, start, previous)


# --- PM-29: what the report says ----------------------------------------------------------------------------------------------------------


def test_the_scope_change_names_exactly_the_two_items_added_mid_sprint(report):
    facts, text, _ = report

    assert [a.item_id for a in facts.added_after_planning] == ["PM-019", "PM-020"]  # the seed's two planted items, and no other
    assert "Added after planning (created more than 2 days after the sprint started): 2." in text
    assert "PM-019 (created 2026-09-12)" in text and "PM-020 (created 2026-09-15)" in text


def test_the_grace_period_is_a_setting_and_moves_the_line_for_what_counts_as_added_after_planning(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_SCOPE_GRACE_DAYS", "6")  # PM-019 was created 5 days after the sprint started; PM-020, 8

    facts = compute_weekly_facts(*snapshots(seeded_db_path))

    assert [a.item_id for a in facts.added_after_planning] == ["PM-020"] and facts.grace_days == 6


def test_progress_is_the_sprints_done_count_by_status_and_what_finished_this_week(report):
    facts, text, _ = report

    assert (facts.sprint.sprint_id, facts.sprint.day_number, facts.sprint.total_days) == ("sprint-13", 12, 14)
    assert (facts.sprint.done_items, facts.sprint.total_items) == (3, 17)
    assert facts.by_status == {"done": 3, "in_review": 2, "in_progress": 6, "blocked": 4, "backlog": 1, "UNMAPPED": 1}
    assert facts.completed_this_week == ["PM-025", "PM-026", "PM-030"]
    assert "Sprint 13 (sprint-13), day 12 of 14: 3 of 17 items done." in text


def test_a_status_the_tracker_does_not_map_is_shown_as_unmapped_with_its_raw_value_and_counted_nowhere_else(report):
    facts, text, _ = report

    assert [(u.item_id, u.detail) for u in facts.unmapped] == [("PM-022", "waiting_on_vendor")]
    assert "PM-022 has a status the tracker does not map: UNMAPPED (the tracker says 'waiting_on_vendor')" in text
    assert "PM-022" not in [b.item_id for b in facts.blocked] and "pending" not in facts.by_status


def test_risks_are_ranked_high_first_and_only_open_ones_count(report):
    facts, _, _ = report

    assert [(r.risk_id, r.severity) for r in facts.top_risks] == [("RISK-002", "high"), ("RISK-001", "medium")]


def test_decisions_needed_are_the_blocked_items_oldest_first_with_how_long(report):
    facts, text, _ = report

    assert [(b.item_id, facts.blocked_days[b.item_id]) for b in facts.blocked] == [("PM-023", 8), ("PM-024", 7), ("PM-014", 4), ("PM-015", 1)]
    assert "- PM-014 blocked since 2026-09-14, 4 days:" in text and "PM-015 blocked since 2026-09-17, 1 day:" in text
    assert "What to decide is not guessed here." in text


def test_velocity_is_this_weeks_completions_against_last_weeks_and_never_claims_a_cause(report):
    facts, text, _ = report

    assert (len(facts.completed_this_week), len(facts.completed_previous_week), facts.velocity_change_percent) == (3, 2, 50)
    assert "Completed this week: 3, against 2 the week before: up 50%." in text
    assert "it does not say what caused the change" in text and "because" not in text.lower()


def test_a_week_after_a_week_of_nothing_completed_has_no_percentage_to_state(seeded_db_path):
    end, start, previous = snapshots(seeded_db_path, end=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))  # before any item exists: nothing completed, either week

    facts = compute_weekly_facts(end, start, previous)
    text = render_weekly_report(facts)

    assert facts.velocity_change_percent is None and "there is no percentage change to state" in text


def test_a_date_no_sprint_covers_says_so_instead_of_inventing_a_scope(seeded_db_path):
    facts = compute_weekly_facts(*snapshots(seeded_db_path, end=datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)))

    assert facts.sprint is None and "No sprint on file covers this date." in render_weekly_report(facts)


def test_the_same_snapshots_always_make_the_same_report(seeded_db_path):
    one, two = (render_weekly_report(compute_weekly_facts(*snapshots(seeded_db_path))) for _ in range(2))

    assert one == two


# --- PM-30: every number recomputes ---------------------------------------------------------------------------------------------------------


def test_every_figure_in_the_report_recomputes_from_the_snapshots(report):
    facts, text, (end, start, previous) = report

    assert check_report(facts, text, end, start, previous) == []
    assert {f.key: f.value for f in facts.figures} == recompute_figures(end, start, previous)  # and the two sets of figures are the same set


def test_a_figure_that_is_wrong_is_named(report):
    facts, text, (end, start, previous) = report
    wrong = facts.model_copy(update={"figures": [f.model_copy(update={"value": f.value + 1}) if f.key == "completed_this_week" else f for f in facts.figures]})

    problems = check_report(wrong, text, end, start, previous)

    assert any("completed_this_week: the report states 4 but the snapshots give 3" in p for p in problems)


def test_a_figure_missing_from_the_report_or_extra_in_it_is_named(report):
    facts, text, (end, start, previous) = report
    without = facts.model_copy(update={"figures": [f for f in facts.figures if f.key != "blocked"]})
    extra = facts.model_copy(update={"figures": [*facts.figures, facts.figures[0].model_copy(update={"key": "invented"})]})

    assert any("blocked: recomputes to 4 but the report does not state it" in p for p in check_report(without, text, end, start, previous))
    assert any("invented: the report states" in p for p in check_report(extra, text, end, start, previous))


def test_a_number_in_the_text_that_was_never_computed_is_caught(report):
    facts, text, (end, start, previous) = report

    tampered = text.replace("3 of 17 items done", "3 of 17 items done, 99% of the way there")

    assert unexplained_numbers(facts, text) == set()  # ids, dates, ranks and the sprint's name are not figures; nothing else is a stray number
    assert unexplained_numbers(facts, tampered) == {99}
    assert any("the text states 99" in p for p in check_report(facts, tampered, end, start, previous))


def test_the_check_reads_the_stored_snapshots_the_report_was_made_from(seeded_db_path):
    made = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, propose=False)

    end = read_snapshot(made.facts.end_taken_at, seeded_db_path)
    start = read_snapshot(made.facts.start_taken_at, seeded_db_path)
    previous = read_snapshot(made.facts.previous_taken_at, seeded_db_path)

    assert made.problems == [] and check_report(made.facts, made.text, end, start, previous) == []


# --- the job: stored snapshots, a proposal that is never sent ------------------------------------------------------------------------------------


def test_the_job_stores_three_snapshots_and_offers_one_proposal_per_week(seeded_db_path):
    first = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ)
    again = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ)

    assert first.created and not again.created and first.proposal_id == again.proposal_id
    proposal = ProposalStore(seeded_db_path).get(first.proposal_id)
    assert proposal.type == WEEKLY_REPORT_PROPOSAL_TYPE and proposal.payload["content"] == first.text
    assert set(proposal.payload["snapshots"]) == {"end", "start", "previous"}
    for taken_at in proposal.payload["snapshots"].values():
        assert read_snapshot(taken_at, seeded_db_path).taken_at == taken_at  # each is stored, and readable on its own


def test_it_can_be_rejected_and_the_rejection_is_recorded(seeded_db_path):
    made = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ)

    result = service.reject(made.proposal_id, approver_id="sharon", policy=service.ApprovalPolicy(approver_ids=frozenset({"sharon"})), db_path=seeded_db_path)

    assert result.outcome == service.REJECTED_OUTCOME and ProposalStore(seeded_db_path).get(made.proposal_id).status == "rejected"


def test_a_report_that_does_not_recompute_is_never_offered(seeded_db_path, monkeypatch):
    monkeypatch.setattr(job, "check_report", lambda *a, **k: ["completed_this_week: the report states 4 but the snapshots give 3"])

    made = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ)

    assert made.proposal_id is None and made.problems and ProposalStore(seeded_db_path).list_by_status("pending") == []


def test_the_report_comes_from_the_stored_snapshots_not_from_whatever_the_tracker_says_now(seeded_db_path):
    import sqlite3

    first = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, propose=False)
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE items SET title = 'CHANGED AFTER THE SNAPSHOT' WHERE id = 'PM-023'")  # not something a snapshot of the past rebuilds
    conn.commit()
    conn.close()

    again = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, propose=False)

    assert again.text == first.text and "CHANGED AFTER THE SNAPSHOT" not in again.text  # it used the copies it had already stored


def test_digits_inside_a_title_quoted_from_the_tracker_or_risk_log_are_not_figures(seeded_db_path):
    """Found live: a real risk is titled '... P2 has its own identity model while P1 uses graph ids', and the check refused the whole report."""
    import sqlite3

    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE items SET title = 'P5 uses 55 graph ids while P9 has 77 of its own' WHERE id = 'PM-023'")
    conn.commit()
    conn.close()
    end, start, previous = snapshots(seeded_db_path)
    facts = compute_weekly_facts(end, start, previous)
    text = render_weekly_report(facts)

    assert "P5 uses 55 graph ids while P9 has 77 of its own" in text
    assert check_report(facts, text, end, start, previous) == []
    assert unexplained_numbers(facts, text + "\nIn short: up 99%.") == {99}  # a number outside the quoted title is still caught
