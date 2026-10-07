"""PM-24: the commitment store, seeded and fed by outcome records, with each follow-up recorded once."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from p1.contracts.outcome_record import EvidenceItem, OutcomeRecord

from pm.commitments.outcomes import (
    ADDED,
    ALREADY_KNOWN,
    CONSENT_WITHHELD,
    NO_AUTHOR,
    NOT_A_COMMITMENT,
    NOT_ON_ROSTER,
    MessageInfo,
    ingest_outcome_record,
    is_commitment,
    load_outcome_file,
    resolve_due,
)
from pm.commitments.store import (
    CANCELLED,
    ESCALATED,
    FULFILLED,
    INGESTED,
    NO_DATE,
    NUDGED,
    OPEN,
    CommitmentNotFoundError,
    CommitmentTracker,
)

REPO = Path(__file__).resolve().parents[2]
ROSTER = {"noah.becker", "wei.chen", "mateo.silva", "olivia.dupont"}


@pytest.fixture()
def tracker(seeded_db_path):
    return CommitmentTracker(seeded_db_path)


# --- the store -------------------------------------------------------------------------------------------------------------


def test_the_seeded_commitments_are_in_the_store_and_open(tracker):
    rows = tracker.list()

    assert len(rows) == 8 and {r.status for r in rows} == {OPEN} and {r.source for r in rows} == {"seed"}
    assert tracker.get(6).member_id == "olivia.dupree" and tracker.get(6).due_date_iso == "2026-09-16" and tracker.get(6).item_id == "PM-014"


def test_the_old_adapter_still_reads_the_table_unchanged(seeded_db_path):
    from pm.adapters.commitments import CommitmentsMock

    assert len(CommitmentsMock(db_path=seeded_db_path).list_commitments()) == 8


def test_filtering_by_person_and_status(tracker):
    assert [c.id for c in tracker.list(member_id="wei.chen")] == [3, 5]
    tracker.close(3, status=FULFILLED, day="2026-09-18")
    assert [c.id for c in tracker.list(status=OPEN, member_id="wei.chen")] == [5]


def test_an_unknown_commitment_is_an_error(tracker):
    with pytest.raises(CommitmentNotFoundError):
        tracker.get(999)


def test_adding_the_same_statement_from_the_same_message_twice_adds_it_once(tracker):
    kwargs = {"member_id": "wei.chen", "text": "I'll have it done by 2026-09-20.", "made_at": "2026-09-18", "source_message_id": "m-1"}

    first, created_first = tracker.add(**kwargs)
    second, created_second = tracker.add(**kwargs)

    assert created_first and not created_second and first.id == second.id and len(tracker.list()) == 9


def test_the_same_text_from_a_different_message_is_a_different_commitment(tracker):
    a, _ = tracker.add(member_id="wei.chen", text="I'll do it.", made_at="2026-09-18", source_message_id="m-1")
    b, created = tracker.add(member_id="wei.chen", text="I'll do it.", made_at="2026-09-19", source_message_id="m-2")

    assert created and a.id != b.id


def test_closing_a_commitment_records_it_and_it_cannot_be_closed_twice(tracker):
    assert tracker.close(1, status=FULFILLED, day="2026-09-18", detail="item done")

    assert tracker.get(1).status == FULFILLED and tracker.get(1).closed_at == "2026-09-18"
    assert tracker.event(1, FULFILLED)["detail"] == "item done"
    assert not tracker.close(1, status=CANCELLED, day="2026-09-19")  # already closed: nothing changes
    assert tracker.get(1).status == FULFILLED


def test_a_commitment_is_closed_as_fulfilled_or_cancelled_only(tracker):
    with pytest.raises(ValueError):
        tracker.close(1, status="open", day="2026-09-18")


def test_each_kind_of_event_happens_once_per_commitment(tracker):
    assert tracker.record_event(2, NUDGED, "2026-09-24", "reminder sent")
    assert not tracker.record_event(2, NUDGED, "2026-09-25", "again")  # the database refuses a second one

    assert tracker.event(2, NUDGED)["day"] == "2026-09-24" and len(tracker.events(2)) == 1
    assert tracker.record_event(2, ESCALATED, "2026-09-30")  # a different kind is fine


def test_it_remembers_who_has_been_escalated_about(tracker):
    assert not tracker.ever_escalated_about("wei.chen")

    tracker.record_event(5, ESCALATED, "2026-09-20")

    assert tracker.ever_escalated_about("wei.chen") and not tracker.ever_escalated_about("noah.becker")


# --- reading a due date --------------------------------------------------------------------------------------------------------

TUE, WED, FRI, SAT = date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 18), date(2026, 9, 19)


@pytest.mark.parametrize("text,made_on,expected", [
    ("I'll have it done by 2026-09-24.", TUE, ("2026-09-24", "2026-09-24")),
    ("will have an update tomorrow", TUE, ("2026-09-16", "tomorrow")),
    ("I'll have the bug fixed by end of week.", TUE, ("2026-09-18", "end of week")),  # that week's Friday
    ("fixed by end of the week", FRI, ("2026-09-18", "end of week")),  # said on the Friday: today
    ("fixed by end of week", SAT, ("2026-09-25", "end of week")),  # said on the weekend: the coming Friday
    ("I'll send it EOD", WED, ("2026-09-16", "today")),
    ("I'll send it tonight", WED, ("2026-09-16", "today")),
    ("I'll finish by Friday", WED, ("2026-09-18", "by friday")),
    ("I'll finish by Monday", WED, ("2026-09-21", "by monday")),
    ("I'll finish by Friday", FRI, ("2026-09-25", "by friday")),  # said on a Friday: the next one, never the same day
    ("I'll get to it when I can", TUE, (None, None)),
], ids=["iso", "tomorrow", "end-of-week", "eow-on-friday", "eow-on-weekend", "eod", "tonight", "by-friday", "by-monday", "by-friday-on-friday", "no-date"])
def test_a_due_date_is_resolved_against_the_day_it_was_said(text, made_on, expected):
    assert resolve_due(text, made_on) == expected


@pytest.mark.parametrize("text,expected", [
    ("I'll have the export bug fixed by end of week.", True),
    ("We will ship the wizard by 2026-09-24.", True),
    ("Started work on the export job today, will have an update tomorrow.", True),
    ("Filter persistence should land by 2026-09-19.", True),
    ("Going to look at the retry queue tomorrow.", True),
    ("Pushed the fix for the billing sync, should resolve the flaky test.", False),
    ("Deployed the onboarding wizard to staging, looks stable so far.", False),
    ("Search index work is blocked: the staging DB migration hasn't run yet.", False),
    ("Open question on whether the export job should handle the null case.", False),
])
def test_what_is_a_commitment_and_what_is_a_status_report(text, expected):
    assert is_commitment(text) is expected


# --- feeding the store from an outcome record --------------------------------------------------------------------------------


def _record(updates=(), decisions=(), blockers=(), questions=(), allowlisted=True, day=date(2026, 9, 18)):
    def items(lines):
        return [EvidenceItem(message_id=mid, text=text, quote=text) for mid, text in lines]
    return OutcomeRecord(schema_version="1.0", channel_id="19:proj-gamma@thread.tacv2", channel_display_name="Project Gamma", date=day,
                         allowlisted=allowlisted, roster=sorted(ROSTER), updates=items(updates), decisions=items(decisions),
                         blockers=items(blockers), questions=items(questions), participation=[], generated_at="2026-09-18T22:00:00+00:00")


MESSAGES = {
    "m-1": MessageInfo("wei.chen", "2026-09-18T09:10:00"), "m-2": MessageInfo("mateo.silva", "2026-09-15T08:00:00"),
    "m-3": MessageInfo(None, "2026-09-18T09:20:00"), "m-4": MessageInfo("sofia.stranger", "2026-09-18T09:30:00"),
    "m-5": MessageInfo("noah.becker", "2026-09-18T10:00:00"),
}


def _ingest(tracker, record, **kw):
    return ingest_outcome_record(record, tracker=tracker, message_info=MESSAGES.get, roster=ROSTER, **kw)


def test_a_promise_in_an_update_becomes_a_commitment_by_the_person_who_said_it(tracker):
    results = _ingest(tracker, _record(updates=[("m-1", "I'll have the export fix done by 2026-09-24.")]))

    (result,) = results
    commitment = tracker.get(result.commitment_id)
    assert result.status == ADDED and commitment.member_id == "wei.chen" and commitment.source == "outcome"
    assert (commitment.due_date_iso, commitment.made_at, commitment.source_message_id) == ("2026-09-24", "2026-09-18", "m-1")
    assert tracker.event(commitment.id, INGESTED)["day"] == "2026-09-18"


def test_a_relative_due_date_is_resolved_against_the_day_the_message_was_posted(tracker):
    _ingest(tracker, _record(updates=[("m-2", "I'll have the reporting dashboard export bug fixed by end of week.")]))

    commitment = tracker.list()[-1]
    assert commitment.made_at == "2026-09-15" and commitment.due_date_iso == "2026-09-18" and commitment.due_date_text == "end of week"


def test_a_status_report_is_not_a_commitment(tracker):
    results = _ingest(tracker, _record(updates=[("m-1", "Pushed the fix for the billing sync, should resolve the flaky test.")]))

    assert [r.status for r in results] == [NOT_A_COMMITMENT] and len(tracker.list()) == 8


def test_decisions_count_but_blockers_and_questions_do_not(tracker):
    results = _ingest(tracker, _record(
        decisions=[("m-1", "We will ship the wizard by 2026-09-25.")],
        blockers=[("m-5", "I'll unblock this by Friday once the migration runs.")],
        questions=[("m-5", "Will the export job be in by 2026-09-30?")]))

    assert [r.status for r in results] == [ADDED] and tracker.list()[-1].text.startswith("We will ship")


def test_reading_the_same_record_again_adds_nothing(tracker):
    record = _record(updates=[("m-1", "I'll have the export fix done by 2026-09-24."), ("m-2", "will have an update tomorrow")])
    _ingest(tracker, record)

    again = _ingest(tracker, record)

    assert [r.status for r in again] == [ALREADY_KNOWN, ALREADY_KNOWN] and len(tracker.list()) == 10
    assert len(tracker.events(tracker.list()[-1].id)) == 1  # and no second "ingested" event


def test_a_record_the_channel_did_not_clear_is_not_read(tracker):
    results = _ingest(tracker, _record(updates=[("m-1", "I'll have the export fix done by 2026-09-24.")], allowlisted=False))

    assert [r.status for r in results] == [CONSENT_WITHHELD] and len(tracker.list()) == 8


def test_a_promise_whose_author_is_unknown_or_not_on_the_project_is_reported_not_added(tracker):
    results = _ingest(tracker, _record(updates=[("m-3", "I'll do it by 2026-09-24."), ("m-4", "I'll do it by 2026-09-24."),
                                                ("m-missing", "I'll do it by 2026-09-24.")]))

    assert [r.status for r in results] == [NO_AUTHOR, NOT_ON_ROSTER, NO_AUTHOR] and len(tracker.list()) == 8


def test_an_item_the_promise_names_is_linked_if_it_exists(tracker):
    _ingest(tracker, _record(updates=[("m-1", "I'll have PM-019 verified by 2026-09-24, and PM-999 too.")]), item_exists=lambda i: i == "PM-019")

    assert tracker.list()[-1].item_id == "PM-019"


def test_a_promise_with_no_date_is_kept_undated_not_guessed(tracker):
    _ingest(tracker, _record(updates=[("m-1", "I'll get to the retry queue when I can.")]))

    commitment = tracker.list()[-1]
    assert commitment.due_date_iso is None and commitment.due_date_text == NO_DATE  # the table needs some due information


def test_the_committed_outcome_fixtures_add_nothing_because_they_hold_no_promises(tracker):
    """The two seeded records are status updates and a record with no consent: reading them is safe and adds nothing."""
    folder = REPO / "src" / "pm" / "seed" / "fixtures" / "outcomes" / "19_proj-gamma_thread.tacv2"
    statuses = []
    for path in sorted(folder.glob("*.json")):
        statuses += [r.status for r in _ingest(tracker, load_outcome_file(path))]

    assert ADDED not in statuses and CONSENT_WITHHELD in statuses and len(tracker.list()) == 8


def test_a_record_file_is_read_back_through_the_same_model(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"channel_id": "x"}')

    with pytest.raises(ValueError):
        load_outcome_file(bad)


def test_nothing_else_in_the_database_is_touched_by_feeding_it(tracker, seeded_db_path):
    before = sqlite3.connect(seeded_db_path).execute("SELECT count(*) FROM items").fetchone()

    _ingest(tracker, _record(updates=[("m-1", "I'll have it done by 2026-09-24.")]))

    assert sqlite3.connect(seeded_db_path).execute("SELECT count(*) FROM items").fetchone() == before


# --- an existing database from before this feature ---------------------------------------------------------------------------------


@pytest.fixture()
def old_database(tmp_path):
    """A database as it was before commitment tracking: migrations 0001 to 0004 only, with the seeded commitments in it."""
    import shutil

    from pm.eval.pristine import build_pristine_database
    from pm.storage.db import MIGRATIONS_DIR, run_migrations

    full = build_pristine_database(tmp_path / "full")
    rows = sqlite3.connect(full).execute(
        "SELECT member_id, item_id, text, due_date_iso, due_date_text, made_at, source_message_id FROM commitments").fetchall()
    older = tmp_path / "old_migrations"
    older.mkdir()
    for name in sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql")):
        if name < "0005":
            shutil.copy(MIGRATIONS_DIR / name, older / name)
    db = tmp_path / "old.db"
    run_migrations(db, older)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executemany("INSERT INTO commitments (member_id, item_id, text, due_date_iso, due_date_text, made_at, source_message_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def test_the_old_database_really_lacks_the_new_columns(old_database):
    columns = [r[1] for r in sqlite3.connect(old_database).execute("PRAGMA table_info(commitments)")]

    assert "status" not in columns and "source" not in columns


def test_opening_the_store_on_an_old_database_upgrades_it_and_keeps_every_commitment(old_database):
    tracker = CommitmentTracker(old_database)

    rows = tracker.list()

    assert len(rows) == 8 and {r.status for r in rows} == {OPEN} and {r.source for r in rows} == {"seed"}
    assert tracker.get(6).text.startswith("Expect the staging DB migration") and tracker.get(6).due_date_iso == "2026-09-16"


def test_the_upgrade_is_additive_and_runs_once(old_database):
    CommitmentTracker(old_database)
    tables = {r[0] for r in sqlite3.connect(old_database).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    applied = [r[0] for r in sqlite3.connect(old_database).execute("SELECT filename FROM schema_migrations")]

    assert {"commitment_events", "nudges", "commitments", "items", "proposals"} <= tables
    CommitmentTracker(old_database)  # opening it again changes nothing
    assert [r[0] for r in sqlite3.connect(old_database).execute("SELECT filename FROM schema_migrations")] == applied
    assert applied.count("0005_commitment_tracking.sql") == 1


def test_the_shared_nudge_ledger_upgrades_an_old_database_too(old_database, tmp_path):
    from pm.commitments.cap import SharedNudgeCap

    cap = SharedNudgeCap(db_path=old_database, cap=1, p1_db_path=tmp_path / "gone.db", p1_required=False)

    assert cap.reading("wei.chen", "2026-09-18").sent_by_p2 == 0  # the nudges table exists now


def test_the_ageing_view_works_on_an_old_database_with_no_manual_step(old_database):
    from datetime import date

    from pm.commitments.ageing import ageing_view

    assert len(ageing_view(tracker=CommitmentTracker(old_database), today=date(2026, 9, 18))) == 8
