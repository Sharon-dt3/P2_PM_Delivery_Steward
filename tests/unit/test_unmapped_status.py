"""PM-31, robustness: a free-text status is surfaced as UNMAPPED with its raw value, never corrected into the nearest status.

Silent coercion is guessing with extra steps. A tracker's own statuses are an exact list; a value that is not on it is not "close to" one that is, however
close it looks ("In Progress", "in-progress" and "done " are all UNMAPPED). It is carried with the value the tracker actually holds, shown in the brief, the
end-of-day diff and the weekly report, and counted under no other heading.

Done when: the planted free-text status (PM-022, "waiting_on_vendor") appears as UNMAPPED in the brief with its raw value.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pm.adapters.tracker import TrackerItem, TrackerMock
from pm.eval.pm12_cases import ScriptedGateway
from pm.eval.pm23_cases import facts_from_structure, facts_from_text
from pm.reporting.end_of_day_facts import (
    NEWLY_BLOCKED,
    OTHER_CHANGES,
    SHIPPED,
    STILL_PENDING,
    compute_end_of_day_facts,
)
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import _render_brief, generate_morning_brief
from pm.seed.build import CANONICAL_STATUSES
from pm.state.diff import compute_delta
from pm.state.snapshot import UNMAPPED, build_current_snapshot, normalize_item

ANCHOR = "2026-09-18T12:00:00+00:00"
_STATUS_SECTIONS = ("what each person delivered", "what each person still has pending", "what each person is blocked on")  # as the prompts name them
PLANTED = "PM-022 (Billing sync vendor API migration): UNMAPPED, the tracker says 'waiting_on_vendor', which is not one of its statuses."


def brief_for(db, taken_at=ANCHOR):
    snapshot = build_current_snapshot(db, taken_at=taken_at, tz_name="Asia/Colombo")
    facts = compute_morning_brief_facts(snapshot)
    return facts, generate_morning_brief(facts, ScriptedGateway())


# --- the acceptance test ----------------------------------------------------------------------------------------------------------------


def test_the_planted_free_text_status_appears_as_unmapped_in_the_brief_with_its_raw_value(seeded_db_path):
    _, brief = brief_for(seeded_db_path)

    olivia = brief.content.split("## Olivia Dupont\n")[1].split("\n\n")[0]
    assert f"- Unmapped: {PLANTED}" in olivia


def test_it_is_not_counted_under_pending_or_any_other_heading(seeded_db_path):
    facts, brief = brief_for(seeded_db_path)
    olivia = next(p for p in facts.people if p.assignee_id == "olivia.dupont")

    assert [u.item_id for u in olivia.unmapped] == ["PM-022"] and olivia.unmapped[0].raw_status == "waiting_on_vendor"
    for bucket in (olivia.delivered, olivia.pending, olivia.blocked):
        assert "PM-022" not in [i.item_id for i in bucket]
    unmapped_line = [line for line in brief.content.splitlines() if "UNMAPPED" in line]
    assert len(unmapped_line) == 1 and "PM-022" in unmapped_line[0]  # said as UNMAPPED once
    assert [line for line in brief.content.splitlines() if "PM-022" in line and "UNMAPPED" not in line] == [
        line for line in brief.content.splitlines() if "PM-022" in line and "Committed" in line]  # elsewhere only as what a commitment is about


def test_the_model_is_never_shown_the_unmapped_item_so_it_cannot_tidy_the_raw_value(seeded_db_path):
    prompts = []

    class Spy(ScriptedGateway):
        def generate(self, prompt, **kwargs):
            prompts.append(prompt)
            return super().generate(prompt, **kwargs)

    facts = compute_morning_brief_facts(build_current_snapshot(seeded_db_path, taken_at=ANCHOR, tz_name="Asia/Colombo"))
    generate_morning_brief(facts, Spy())

    assert prompts
    assert not any("waiting_on_vendor" in p for p in prompts)  # the raw value never reaches a model
    for prompt in prompts:  # and the item is never offered under a status heading
        if any(f'"{label}" section' in prompt for label in _STATUS_SECTIONS):
            assert "PM-022" not in prompt


def test_someone_whose_only_item_is_unmapped_is_not_reported_as_having_no_activity(seeded_db_path):
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE items SET assignee_id = 'aisha.rahman' WHERE id = 'PM-022'")
    conn.execute("DELETE FROM commits WHERE author_id = 'aisha.rahman'")
    conn.execute("DELETE FROM item_comments WHERE author_id = 'aisha.rahman'")
    for item in ("PM-009", "PM-011", "PM-017", "PM-030"):
        conn.execute("UPDATE items SET assignee_id = NULL WHERE id = ?", (item,))
    conn.commit()
    conn.close()

    facts, brief = brief_for(seeded_db_path)
    aisha = next(p for p in facts.people if p.assignee_id == "aisha.rahman")
    section = brief.content.split("## Aisha Rahman\n")[1].split("\n\n")[0]

    assert aisha.has_activity and "No update" not in section and "Unmapped: PM-022" in section


# --- never corrected into the nearest status ----------------------------------------------------------------------------------------------


NEAR_MISSES = ["In Progress", "in-progress", "IN_PROGRESS", "in progress", "inprogress", "in_progres", "done ", " done", "Done", "DONE", "blocker", "Blocked",
               "in_review ", "In Review", "backlog.", "Backlog", "wip", "doing", "closed", "to do", "todo", "", " ", "in_progress\n", "in_progrés", "waiting_on_vendor"]


@pytest.mark.parametrize("raw", NEAR_MISSES)
def test_a_status_that_is_not_exactly_one_of_the_trackers_is_unmapped_and_keeps_its_raw_value(raw):
    item = normalize_item(TrackerItem(id="PM-100", title="x", status=raw, sprint_id="sprint-13", created_at="2026-09-10"))

    assert item.status == UNMAPPED and item.raw_status == raw  # exactly as the tracker holds it: not stripped, not re-cased, not corrected


@pytest.mark.parametrize("canonical", sorted(CANONICAL_STATUSES))
def test_the_trackers_own_statuses_pass_through_untouched(canonical):
    item = normalize_item(TrackerItem(id="PM-100", title="x", status=canonical, sprint_id="sprint-13", created_at="2026-09-10"))

    assert item.status == canonical and item.raw_status == canonical


@pytest.mark.parametrize("raw", ["In Progress", "done ", "it's waiting on legal", 'said "blocked"', "a\nb"])
def test_each_near_miss_reaches_the_brief_as_unmapped_with_exactly_that_raw_value(seeded_db_path, raw):
    TrackerMock(db_path=seeded_db_path).transition("PM-029", raw)  # a real status change on a real item, to a value the tracker does not map

    facts, brief = brief_for(seeded_db_path, taken_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    olivia = next(p for p in facts.people if p.assignee_id == "olivia.dupont")
    shown = brief.content.split("## Olivia Dupont\n")[1].split("\n\n")[0]

    assert [u.raw_status for u in olivia.unmapped if u.item_id == "PM-029"] == [raw]
    assert f"PM-029 (Caching layer TTL tuning): UNMAPPED, the tracker says {raw!r}, which is not one of its statuses." in shown
    assert "PM-029" not in [i.item_id for i in olivia.pending]


def test_what_the_brief_shows_is_checked_against_the_facts_so_a_dropped_or_changed_raw_value_is_caught(seeded_db_path):
    facts, brief = brief_for(seeded_db_path)
    expected = facts_from_structure(facts)

    assert ("unmapped", "Olivia Dupont", "PM-022", "waiting_on_vendor") in expected and facts_from_text(brief.content) == expected
    assert facts_from_text(brief.content.replace("'waiting_on_vendor'", "'in_progress'")) != expected  # tidied into a real status
    assert facts_from_text(brief.content.replace(PLANTED, "")) != expected  # dropped altogether
    assert not any(f[0] == "pending" for f in expected)


# --- the same value in the end-of-day diff ----------------------------------------------------------------------------------------------------


def two_snapshots(db):
    now = datetime.now(timezone.utc)
    return build_current_snapshot(db, taken_at=(now - timedelta(hours=1)).isoformat(), tz_name="UTC"), lambda: build_current_snapshot(
        db, taken_at=(now + timedelta(hours=1)).isoformat(), tz_name="UTC")


def delta_after(db, item_id, *statuses):
    before, take_after = two_snapshots(db)
    tracker = TrackerMock(db_path=db)
    for status in statuses:
        tracker.transition(item_id, status)
    after = take_after()
    return compute_delta(before, after, tracker), before, after


def test_a_move_into_an_unmapped_status_is_reported_with_the_raw_value(seeded_db_path):
    delta, *_ = delta_after(seeded_db_path, "PM-017", "waiting_on_legal")
    entry = next(e for e in delta.items if e.item_id == "PM-017")

    assert (entry.before_status, entry.after_status, entry.after_raw) == ("in_progress", "UNMAPPED", "waiting_on_legal")
    assert "PM-017 moved from in_progress to UNMAPPED ('waiting_on_legal')." in entry.description


def test_a_move_between_two_different_unmapped_values_is_a_change_not_unmapped_to_unmapped(seeded_db_path):
    delta, *_ = delta_after(seeded_db_path, "PM-022")  # PM-022 is already 'waiting_on_vendor' at the start
    assert [e for e in delta.items if e.item_id == "PM-022"] == []  # unchanged: nothing to report

    delta, *_ = delta_after(seeded_db_path, "PM-022", "waiting_on_legal")
    entry = next(e for e in delta.items if e.item_id == "PM-022")

    assert (entry.before_raw, entry.after_raw, entry.kind) == ("waiting_on_vendor", "waiting_on_legal", "status_changed")
    assert "PM-022 moved from UNMAPPED ('waiting_on_vendor') to UNMAPPED ('waiting_on_legal')." in entry.description


def test_a_move_out_of_an_unmapped_status_names_the_raw_value_it_left(seeded_db_path):
    delta, *_ = delta_after(seeded_db_path, "PM-022", "in_progress")
    entry = next(e for e in delta.items if e.item_id == "PM-022")

    assert (entry.before_raw, entry.after_raw) == ("waiting_on_vendor", None)
    assert "PM-022 moved from UNMAPPED ('waiting_on_vendor') to in_progress." in entry.description


def test_a_churn_that_ends_unmapped_says_so_with_the_raw_value(seeded_db_path):
    delta, *_ = delta_after(seeded_db_path, "PM-017", "in_review", "waiting_on_legal")
    entry = next(e for e in delta.items if e.item_id == "PM-017")

    assert entry.after_raw == "waiting_on_legal" and "UNMAPPED ('waiting_on_legal')" in entry.description


def test_the_end_of_day_summary_does_not_file_an_unmapped_move_as_shipped_blocked_or_pending(seeded_db_path):
    delta, before, after = delta_after(seeded_db_path, "PM-017", "waiting_on_legal")

    eod = compute_end_of_day_facts(delta, before, after)
    where = next(key for key, items in eod.sections.items() if any(i.item_id == "PM-017" for i in items))
    item = next(i for i in eod.sections[where] if i.item_id == "PM-017")

    assert where == OTHER_CHANGES and where not in (SHIPPED, NEWLY_BLOCKED, STILL_PENDING)
    assert "UNMAPPED ('waiting_on_legal')" in item.detail


def test_a_new_or_removed_item_with_an_unmapped_status_carries_the_raw_value(seeded_db_path):
    now = datetime.now(timezone.utc)
    before = build_current_snapshot(seeded_db_path, taken_at=(now - timedelta(hours=1)).isoformat(), tz_name="UTC")
    tracker = TrackerMock(db_path=seeded_db_path)
    tracker.create_item(TrackerItem(id="PM-200", title="New work", status="parked", sprint_id="sprint-13", created_at=(now - timedelta(minutes=5)).isoformat()))
    after = build_current_snapshot(seeded_db_path, taken_at=(now + timedelta(hours=1)).isoformat(), tz_name="UTC")

    added = next(e for e in compute_delta(before, after, tracker).items if e.item_id == "PM-200")
    removed = next(e for e in compute_delta(after, before, tracker).items if e.item_id == "PM-200")

    assert "PM-200 is new since the previous snapshot (now UNMAPPED ('parked'))." == added.description and added.after_raw == "parked"
    assert "PM-200 is no longer present in the tracker (was UNMAPPED ('parked'))." == removed.description and removed.before_raw == "parked"


# --- the unassigned list, and the weekly report ------------------------------------------------------------------------------------------------


def test_an_unassigned_item_with_an_unmapped_status_shows_the_raw_value_too(seeded_db_path):
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE items SET status = 'parked' WHERE id = 'PM-018'")
    conn.commit()
    conn.close()

    facts, brief = brief_for(seeded_db_path, taken_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())

    assert "- PM-018 (Caching layer PR review): nobody is assigned; status UNMAPPED (the tracker says 'parked')." in brief.content
    assert facts_from_text(brief.content) == facts_from_structure(facts)


def test_the_weekly_report_says_it_the_same_way(seeded_db_path):
    from pm.jobs.weekly_report_job import WEEK
    from pm.reporting.weekly import compute_weekly_facts, render_weekly_report

    end = datetime(2026, 9, 18, 12, 30, tzinfo=timezone.utc)
    facts = compute_weekly_facts(*(build_current_snapshot(seeded_db_path, taken_at=(end - n * WEEK).isoformat(), tz_name="Asia/Colombo") for n in (0, 1, 2)))

    assert "PM-022 has a status the tracker does not map: UNMAPPED (the tracker says 'waiting_on_vendor')." in render_weekly_report(facts)


def test_the_rendering_is_deterministic_the_same_facts_make_the_same_unmapped_line(seeded_db_path):
    facts, _ = brief_for(seeded_db_path)
    sections = {k: [] for k in ("sprint_scope", "committed", "delivered", "pending", "blocked", "blockers")}

    assert _render_brief(facts, sections) == _render_brief(facts, sections)


def test_the_end_of_day_footer_does_not_count_an_unmapped_item_as_open_and_names_it_with_its_raw_value(seeded_db_path):
    delta, before, after = delta_after(seeded_db_path, "PM-017", "in_review")  # an ordinary change elsewhere; PM-022 is untouched and unmapped

    eod = compute_end_of_day_facts(delta, before, after)

    assert eod.unchanged_unmapped == ["PM-022 ('waiting_on_vendor')"]
    open_ids = {i.id for i in after.items if i.status not in ("done", UNMAPPED)} - {"PM-017"}
    assert eod.unchanged_open_count == len(open_ids)  # PM-022 is in neither count: it is not known to be open


def test_the_footer_counts_them_in_the_summary_without_naming_them_because_the_summary_names_only_what_changed(seeded_db_path):
    from pm.reporting.end_of_day_summary import generate_end_of_day_summary
    from pm.reporting.scripted_summary import ScriptedSummaryGateway

    delta, before, after = delta_after(seeded_db_path, "PM-017", "in_review")
    summary = generate_end_of_day_summary(compute_end_of_day_facts(delta, before, after), ScriptedSummaryGateway())

    assert summary.content.splitlines()[-1] == "1 other item has a status the tracker does not map (UNMAPPED) and is not counted as open."
    assert "PM-022" not in summary.content and "waiting_on_vendor" not in summary.content  # unchanged items are not named here (PM-22)


def test_the_footer_line_is_left_out_when_nothing_is_unmapped(seeded_db_path):
    from pm.reporting.end_of_day_summary import generate_end_of_day_summary
    from pm.reporting.scripted_summary import ScriptedSummaryGateway

    TrackerMock(db_path=seeded_db_path).transition("PM-022", "in_progress")
    delta, before, after = delta_after(seeded_db_path, "PM-017", "in_review")
    summary = generate_end_of_day_summary(compute_end_of_day_facts(delta, before, after), ScriptedSummaryGateway())

    assert "UNMAPPED" not in summary.content.splitlines()[-1] and summary.content.splitlines()[-1].endswith("did not change today.")
