"""Adding a sprint and moving items into it: checked, atomic, and audited. Nothing picks a sprint on the operator's behalf.

A sprint keeps the weekly report and the channel batches right: with none covering the day, the report has nothing to measure and new items fall back to
the named default. The command is how the next sprint gets added in one line.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pm.jobs.weekly_report_job import WEEK
from pm.reporting.weekly import compute_weekly_facts
from pm.state.snapshot import build_current_snapshot
from pm.tracker_admin import TrackerAdminRefused, add_sprint, move_items


@pytest.fixture()
def script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "add_sprint.py"
    spec = importlib.util.spec_from_file_location("add_sprint_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sprints(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT id, display_name, start_date, end_date FROM sprints ORDER BY start_date").fetchall()
    finally:
        conn.close()


def audit(db, action):
    conn = sqlite3.connect(db)
    try:
        return [(a, e, json.loads(d)) for a, e, d in conn.execute("SELECT actor, entity_id, details FROM audit WHERE action = ?", (action,))]
    finally:
        conn.close()


def sprint_of(db, item_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT sprint_id FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    finally:
        conn.close()


NEW = {"sprint_id": "sprint-14", "name": "Sprint 14", "start": "2026-10-05", "end": "2026-10-18"}


# --- add_sprint ---------------------------------------------------------------------------------------------------------------------------


def test_a_sprint_is_added_with_its_dates_and_the_audit_says_who_and_what(seeded_db_path):
    add_sprint(seeded_db_path, **NEW, by="sharon")

    assert sprints(seeded_db_path)[-1] == ("sprint-14", "Sprint 14", "2026-10-05", "2026-10-18")
    assert audit(seeded_db_path, "sprint.added") == [("sharon", "sprint-14", {"name": "Sprint 14", "start": "2026-10-05", "end": "2026-10-18"})]


@pytest.mark.parametrize(("change", "why"), [
    ({"sprint_id": "Sprint 14"}, "lower-case letters"),
    ({"sprint_id": ""}, "lower-case letters"),
    ({"name": "  "}, "needs a name"),
    ({"start": "5 October"}, "the start must be a date"),
    ({"end": "2026-13-45"}, "the end must be a date"),
    ({"start": "2026-10-19", "end": "2026-10-18"}, "ends (2026-10-18) before it starts"),
    ({"sprint_id": "sprint-13"}, "already exists"),
])
def test_a_bad_sprint_is_refused_with_the_reason_and_nothing_is_added(seeded_db_path, change, why):
    before = sprints(seeded_db_path)

    with pytest.raises(TrackerAdminRefused, match=re.escape(why)):
        add_sprint(seeded_db_path, **{**NEW, **change})

    assert sprints(seeded_db_path) == before and audit(seeded_db_path, "sprint.added") == []


@pytest.mark.parametrize(("start", "end"), [
    ("2026-09-20", "2026-10-03"),  # starts on the last day of Sprint 13
    ("2026-09-01", "2026-09-07"),  # ends on Sprint 13's first day
    ("2026-09-10", "2026-09-12"),  # inside Sprint 13
    ("2026-08-01", "2026-10-30"),  # swallows both
])
def test_a_range_that_shares_a_day_with_another_sprint_is_refused(seeded_db_path, start, end):
    with pytest.raises(TrackerAdminRefused, match="overlaps"):
        add_sprint(seeded_db_path, **{**NEW, "start": start, "end": end})


def test_the_day_after_a_sprint_ends_is_free(seeded_db_path):
    add_sprint(seeded_db_path, **{**NEW, "start": "2026-09-21", "end": "2026-10-04"})

    assert len(sprints(seeded_db_path)) == 3


def test_two_sprints_can_be_added_one_after_the_other_and_not_the_same_twice(seeded_db_path):
    add_sprint(seeded_db_path, **NEW)
    add_sprint(seeded_db_path, sprint_id="sprint-15", name="Sprint 15", start="2026-10-19", end="2026-11-01")

    assert [s[0] for s in sprints(seeded_db_path)][-2:] == ["sprint-14", "sprint-15"]
    with pytest.raises(TrackerAdminRefused, match="already exists"):
        add_sprint(seeded_db_path, **NEW)


# --- move_items ----------------------------------------------------------------------------------------------------------------------------


def test_items_are_moved_into_the_sprint_and_each_move_is_audited(seeded_db_path):
    add_sprint(seeded_db_path, **NEW)

    result = move_items(seeded_db_path, ["PM-014", "PM-015"], sprint_id="sprint-14", by="sharon")

    assert result.moved == ["PM-014", "PM-015"] and sprint_of(seeded_db_path, "PM-014") == sprint_of(seeded_db_path, "PM-015") == "sprint-14"
    assert audit(seeded_db_path, "item.moved_sprint") == [("sharon", "PM-014", {"from": "sprint-13", "to": "sprint-14"}), ("sharon", "PM-015", {"from": "sprint-13", "to": "sprint-14"})]


def test_an_item_already_in_the_sprint_is_left_alone_and_not_audited_again(seeded_db_path):
    add_sprint(seeded_db_path, **NEW)
    move_items(seeded_db_path, ["PM-014"], sprint_id="sprint-14")

    again = move_items(seeded_db_path, ["PM-014", "PM-015"], sprint_id="sprint-14")

    assert again.moved == ["PM-015"] and again.already_there == ["PM-014"]
    assert [e for _, e, _ in audit(seeded_db_path, "item.moved_sprint")] == ["PM-014", "PM-015"]


def test_one_unknown_item_means_nothing_is_moved(seeded_db_path):
    add_sprint(seeded_db_path, **NEW)

    with pytest.raises(TrackerAdminRefused, match="no item 'PM-999'; nothing was moved"):
        move_items(seeded_db_path, ["PM-014", "PM-999"], sprint_id="sprint-14")

    assert sprint_of(seeded_db_path, "PM-014") == "sprint-13" and audit(seeded_db_path, "item.moved_sprint") == []


def test_moving_into_a_sprint_that_does_not_exist_is_refused(seeded_db_path):
    with pytest.raises(TrackerAdminRefused, match="no sprint 'sprint-99'"):
        move_items(seeded_db_path, ["PM-014"], sprint_id="sprint-99")

    assert sprint_of(seeded_db_path, "PM-014") == "sprint-13"


# --- the command ---------------------------------------------------------------------------------------------------------------------------


def test_the_command_adds_the_sprint_and_moves_the_items_and_says_so(script, seeded_db_path, capsys):
    status = script.main(["--id", "sprint-14", "--name", "Sprint 14", "--start", "2026-10-05", "--end", "2026-10-18", "--move", "PM-014, PM-015",
                          "--by", "sharon", "--db", str(seeded_db_path)])

    out = capsys.readouterr().out
    assert status == 0 and "added Sprint 14 (sprint-14), 2026-10-05 to 2026-10-18" in out and "moved: PM-014, PM-015" in out
    assert sprint_of(seeded_db_path, "PM-015") == "sprint-14"


def test_a_dry_run_says_what_would_happen_and_changes_nothing(script, seeded_db_path, capsys):
    before = (sprints(seeded_db_path), sprint_of(seeded_db_path, "PM-014"))

    status = script.main(["--id", "sprint-14", "--name", "Sprint 14", "--start", "2026-10-05", "--end", "2026-10-18", "--move", "PM-014",
                          "--db", str(seeded_db_path), "--dry-run"])

    assert status == 0 and "dry run, nothing changed: would add Sprint 14" in capsys.readouterr().out
    assert (sprints(seeded_db_path), sprint_of(seeded_db_path, "PM-014")) == before and audit(seeded_db_path, "sprint.added") == []


def test_a_dry_run_refuses_what_the_real_run_would_refuse(script, seeded_db_path, capsys):
    status = script.main(["--id", "sprint-14", "--name", "Sprint 14", "--start", "2026-09-10", "--end", "2026-09-12", "--db", str(seeded_db_path), "--dry-run"])

    out = capsys.readouterr().out
    assert status == 1 and "not done:" in out and "overlaps Sprint 13" in out


def test_the_command_exits_with_an_error_and_says_why_when_it_refuses(script, seeded_db_path, capsys):
    status = script.main(["--id", "sprint-13", "--name", "Again", "--start", "2026-11-01", "--end", "2026-11-14", "--db", str(seeded_db_path)])

    assert status == 1 and "not done: sprint-13 already exists" in capsys.readouterr().out


# --- what it fixes -------------------------------------------------------------------------------------------------------------------------


def test_with_a_current_sprint_the_weekly_report_has_something_to_measure(seeded_db_path):
    thursday = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)

    def facts():
        return compute_weekly_facts(*(build_current_snapshot(seeded_db_path, taken_at=(thursday - n * WEEK).isoformat(), tz_name="Asia/Colombo") for n in (0, 1, 2)))

    assert facts().sprint is None  # nothing covers 8 October: the report has no scope to speak of

    add_sprint(seeded_db_path, **NEW)
    move_items(seeded_db_path, ["PM-014", "PM-015"], sprint_id="sprint-14")
    after = facts()

    assert (after.sprint.sprint_id, after.sprint.day_number, after.sprint.total_days, after.sprint.total_items) == ("sprint-14", 4, 14, 2)
    assert [b.item_id for b in after.blocked] == ["PM-014", "PM-015"]
