"""PM-24: the per-person per-day nudge cap, shared with P1 so one person is never chased by two agents on one day."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pm.commitments.cap import (
    SharedCapUnavailableError,
    SharedNudgeCap,
    cap_from_environment,
    p1_ledger_path,
)

DAY = "2026-09-18"


def _p1_ledger(path: Path, rows=()):
    """A database with P1's own nudges table (its real shape), holding the given (channel, member, date, sent_at) rows."""
    conn = sqlite3.connect(path)
    conn.executescript(Path(__file__).resolve().parents[2].joinpath("..", "P3_Agents", "src", "p1", "storage", "migrations", "0004_nudges.sql").read_text()
                       .replace("REFERENCES channels(id)", "").replace("REFERENCES members(id)", ""))
    for i, (channel, member, date, sent_at) in enumerate(rows):
        conn.execute("INSERT INTO nudges (channel_id, member_id, date, sent_at, idempotency_key) VALUES (?, ?, ?, ?, ?)",
                     (channel, member, date, sent_at, f"k{i}"))
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def p1(tmp_path):
    return tmp_path / "p1.db"


def _cap(seeded_db_path, p1, cap=1):
    return SharedNudgeCap(db_path=seeded_db_path, cap=cap, p1_db_path=p1)


def _send(cap_obj, member, day=DAY, n=1):
    for i in range(n):
        key = f"p2:{member}:{day}:{i}"
        cap_obj.record(member_id=member, day=day, commitment_id=None, proposal_id=None, key=key)
        cap_obj.mark_sent(key, sent_at=f"{day}T09:00:00+00:00", day=day)


# --- the point of it ---------------------------------------------------------------------------------------------------------


def test_a_person_p1_already_nudged_today_is_not_nudged_by_this_agent(seeded_db_path, p1):
    _p1_ledger(p1, [("19:p1-agent-test@thread.tacv2", "wei.chen", DAY, f"{DAY}T10:00:00+00:00")])

    reading = _cap(seeded_db_path, p1).reading("wei.chen", DAY)

    assert reading.sent_by_p1 == 1 and reading.sent_by_p2 == 0 and reading.reached
    assert not _cap(seeded_db_path, p1).allows("wei.chen", DAY)


def test_a_person_this_agent_already_nudged_today_counts_too(seeded_db_path, p1):
    _p1_ledger(p1)
    cap = _cap(seeded_db_path, p1)

    _send(cap, "wei.chen")

    assert cap.reading("wei.chen", DAY).sent_by_p2 == 1 and not cap.allows("wei.chen", DAY)  # so P1 reading this ledger would see it


def test_p1s_nudges_in_any_channel_count_not_just_one(seeded_db_path, p1):
    """P1's own cap is per channel; the shared one is per person: a nudge in a different P1 channel still counts."""
    _p1_ledger(p1, [("19:some-other-channel@thread.tacv2", "wei.chen", DAY, f"{DAY}T10:00:00+00:00")])

    assert _cap(seeded_db_path, p1).reading("wei.chen", DAY).sent_by_p1 == 1


def test_the_two_agents_nudges_add_up(seeded_db_path, p1):
    _p1_ledger(p1, [("19:a@thread.tacv2", "wei.chen", DAY, f"{DAY}T10:00:00+00:00")])
    cap = _cap(seeded_db_path, p1, cap=2)
    assert cap.allows("wei.chen", DAY)  # 1 of 2 so far

    _send(cap, "wei.chen")

    reading = cap.reading("wei.chen", DAY)
    assert (reading.sent_by_p1, reading.sent_by_p2, reading.total) == (1, 1, 2) and reading.reached  # together they hit the cap


def test_only_delivered_nudges_count(seeded_db_path, p1):
    """A nudge waiting for approval, or rejected, was never delivered: it must not use up the person's day."""
    _p1_ledger(p1, [("19:a@thread.tacv2", "wei.chen", DAY, None)])
    cap = _cap(seeded_db_path, p1)
    cap.record(member_id="wei.chen", day=DAY, commitment_id=None, proposal_id="p-1", key="pending")  # planned, not sent

    assert cap.reading("wei.chen", DAY).total == 0 and cap.allows("wei.chen", DAY)


def test_it_is_per_person_and_per_day(seeded_db_path, p1):
    _p1_ledger(p1, [("19:a@thread.tacv2", "wei.chen", DAY, f"{DAY}T10:00:00+00:00")])
    cap = _cap(seeded_db_path, p1)

    assert cap.allows("noah.becker", DAY)  # someone else
    assert cap.allows("wei.chen", "2026-09-19")  # tomorrow
    assert cap.allows("wei.chen", "2026-09-17")  # yesterday: the ledger is by day


def test_a_larger_cap_allows_more(seeded_db_path, p1):
    _p1_ledger(p1)
    cap = _cap(seeded_db_path, p1, cap=2)

    _send(cap, "wei.chen")
    assert cap.allows("wei.chen", DAY)  # one of two
    cap.record(member_id="wei.chen", day=DAY, commitment_id=None, proposal_id=None, key="second")
    cap.mark_sent("second", sent_at=f"{DAY}T11:00:00+00:00", day=DAY)

    assert not cap.allows("wei.chen", DAY)


def test_a_cap_of_zero_means_no_nudges_at_all(seeded_db_path, p1):
    _p1_ledger(p1)

    assert not _cap(seeded_db_path, p1, cap=0).allows("wei.chen", DAY)


# --- this agent never writes to P1 -------------------------------------------------------------------------------------------


def test_p1s_ledger_is_only_ever_read(seeded_db_path, p1):
    _p1_ledger(p1, [("19:a@thread.tacv2", "wei.chen", DAY, f"{DAY}T10:00:00+00:00")])
    before = p1.read_bytes()
    cap = _cap(seeded_db_path, p1)

    cap.reading("wei.chen", DAY)
    _send(cap, "noah.becker")

    assert p1.read_bytes() == before  # not a byte of it changed


def test_it_is_opened_read_only_so_it_could_not_write_even_if_it_tried(seeded_db_path, p1):
    _p1_ledger(p1)

    conn = sqlite3.connect(f"file:{p1}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO nudges (channel_id, member_id, date, idempotency_key) VALUES ('c', 'm', 'd', 'k')")


# --- it fails closed ------------------------------------------------------------------------------------------------------------


def test_an_unreadable_p1_ledger_means_nobody_is_nudged(seeded_db_path, tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_text("this is not a database")

    with pytest.raises(SharedCapUnavailableError):
        _cap(seeded_db_path, broken).allows("wei.chen", DAY)


def test_a_p1_ledger_without_the_table_is_unreadable_not_empty(seeded_db_path, tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    with pytest.raises(SharedCapUnavailableError):
        _cap(seeded_db_path, empty).reading("wei.chen", DAY)


def test_a_configured_p1_ledger_that_is_missing_is_an_error(seeded_db_path, tmp_path):
    with pytest.raises(SharedCapUnavailableError):
        _cap(seeded_db_path, tmp_path / "gone.db").reading("wei.chen", DAY)


def test_when_p1_has_never_run_here_its_count_is_zero_and_the_reading_says_so(seeded_db_path, tmp_path):
    cap = SharedNudgeCap(db_path=seeded_db_path, cap=1, p1_db_path=tmp_path / "gone.db", p1_required=False)

    reading = cap.reading("wei.chen", DAY)

    assert reading.sent_by_p1 == 0 and reading.p1_ledger == "absent" and cap.allows("wei.chen", DAY)


# --- configuration ---------------------------------------------------------------------------------------------------------------


def test_the_p1_ledger_is_found_from_the_environment_or_next_to_the_p1_repo():
    explicit, flagged = p1_ledger_path({"P1_DB_PATH": "/somewhere/p1.db"})
    default, default_flagged = p1_ledger_path({})

    assert (explicit, flagged) == (Path("/somewhere/p1.db"), True)
    assert default.name == "p1_live.db" and default.parent.name == "data" and not default_flagged


def test_the_cap_comes_from_the_environment_when_set():
    assert cap_from_environment(1, {}) == 1 and cap_from_environment(1, {"PM_NUDGE_CAP_PER_PERSON_PER_DAY": "3"}) == 3
    assert cap_from_environment(2, {"PM_NUDGE_CAP_PER_PERSON_PER_DAY": " "}) == 2


@pytest.mark.parametrize("bad", ["two", "-1", "1.5"])
def test_a_bad_cap_is_an_error_not_a_default(bad):
    with pytest.raises(ValueError):
        cap_from_environment(1, {"PM_NUDGE_CAP_PER_PERSON_PER_DAY": bad})


def test_the_ledger_remembers_who_has_ever_been_nudged(seeded_db_path, p1):
    _p1_ledger(p1)
    cap = _cap(seeded_db_path, p1)
    assert not cap.ever_nudged("wei.chen")

    cap.record(member_id="wei.chen", day=DAY, commitment_id=None, proposal_id=None, key="k")
    assert not cap.ever_nudged("wei.chen")  # planned is not delivered

    cap.mark_sent("k", sent_at=f"{DAY}T09:00:00+00:00", day=DAY)
    assert cap.ever_nudged("wei.chen") and not cap.ever_nudged("noah.becker")


def test_marking_it_sent_moves_it_to_the_day_it_actually_went(seeded_db_path, p1):
    """Planned on the 18th, approved and sent on the 19th: it uses up the 19th, not the 18th."""
    _p1_ledger(p1)
    cap = _cap(seeded_db_path, p1)
    cap.record(member_id="wei.chen", day="2026-09-18", commitment_id=None, proposal_id=None, key="k")

    cap.mark_sent("k", sent_at="2026-09-19T09:00:00+00:00", day="2026-09-19")

    assert cap.allows("wei.chen", "2026-09-18") and not cap.allows("wei.chen", "2026-09-19")


# --- the same person under different ids ------------------------------------------------------------------------------------------
# P1's live ledger names people by their Microsoft Graph user id; P2's roster uses its own ids. Matching by id alone would
# silently miss, so the cap resolves a person to every id they appear under in P1 by their display name.


def _p1_with_members(path, members, nudges):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE members (id TEXT PRIMARY KEY, display_name TEXT, email TEXT, tenant_status TEXT)")
    conn.execute("CREATE TABLE nudges (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL, member_id TEXT NOT NULL, "
                 "date TEXT NOT NULL, proposal_id TEXT, sent_at TEXT, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT)")
    conn.executemany("INSERT INTO members (id, display_name) VALUES (?, ?)", members)
    for i, (member, day, sent) in enumerate(nudges):
        conn.execute("INSERT INTO nudges (channel_id, member_id, date, sent_at, idempotency_key) VALUES ('19:p1@thread.tacv2', ?, ?, ?, ?)",
                     (member, day, sent, f"k{i}"))
    conn.commit()
    conn.close()
    return path


def test_a_person_p1_knows_by_a_different_id_still_counts(seeded_db_path, p1):
    """wei.chen is 'Wei Chen' in P2's roster; P1 nudged the Graph user 'aad-1111' whose name is 'Wei Chen'."""
    _p1_with_members(p1, [("aad-1111", "Wei Chen"), ("aad-2222", "Someone Else")], [("aad-1111", DAY, f"{DAY}T10:00:00+00:00")])
    cap = _cap(seeded_db_path, p1)

    reading = cap.reading("wei.chen", DAY)

    assert reading.sent_by_p1 == 1 and reading.reached and not cap.allows("wei.chen", DAY)
    assert cap.allows("noah.becker", DAY)  # and someone else is not caught up in it


def test_the_name_match_ignores_case_and_spacing(seeded_db_path, p1):
    _p1_with_members(p1, [("aad-1111", "  wei CHEN ")], [("aad-1111", DAY, f"{DAY}T10:00:00+00:00")])

    assert _cap(seeded_db_path, p1).reading("wei.chen", DAY).sent_by_p1 == 1


def test_two_people_in_p1_with_the_same_name_are_both_counted_the_safe_way(seeded_db_path, p1):
    _p1_with_members(p1, [("aad-1", "Wei Chen"), ("aad-2", "Wei Chen")],
                     [("aad-1", DAY, f"{DAY}T10:00:00+00:00"), ("aad-2", DAY, f"{DAY}T11:00:00+00:00")])

    assert _cap(seeded_db_path, p1).reading("wei.chen", DAY).sent_by_p1 == 2  # not knowing which is which means counting both


def test_a_person_p1_has_no_one_of_that_name_for_is_not_counted(seeded_db_path, p1):
    _p1_with_members(p1, [("aad-9", "A Stranger")], [("aad-9", DAY, f"{DAY}T10:00:00+00:00")])

    assert _cap(seeded_db_path, p1).reading("wei.chen", DAY).sent_by_p1 == 0


def test_the_same_id_in_both_systems_still_matches_without_a_members_table(seeded_db_path, p1):
    """P1's mock channels use the same ids as P2: an exact id match needs no name lookup, and no members table is not an error."""
    _p1_ledger(p1, [("19:a@thread.tacv2", "wei.chen", DAY, f"{DAY}T10:00:00+00:00")])

    assert _cap(seeded_db_path, p1).reading("wei.chen", DAY).sent_by_p1 == 1


def test_a_nudge_p1_sent_to_the_id_and_another_to_the_name_match_both_count(seeded_db_path, p1):
    _p1_with_members(p1, [("aad-1", "Wei Chen"), ("wei.chen", "Wei Chen")],
                     [("aad-1", DAY, f"{DAY}T10:00:00+00:00"), ("wei.chen", DAY, f"{DAY}T11:00:00+00:00")])

    assert _cap(seeded_db_path, p1).reading("wei.chen", DAY).sent_by_p1 == 2
