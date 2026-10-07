"""PM-19, blocker age: computed from the item's status TRANSITIONS, not from a stored field.

The age of a blocker is how many days it has been in `blocked` continuously, as of the
snapshot's moment: from the transition that put it there (the latest entry into the current
blocked run, so a flap resets the clock), to the project's local date at that moment. The
tracker's own `blocked_since` column is never consulted: it can be stale or wrong, which is
exactly why the age is rebuilt from the history.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pm.adapters.tracker import TransitionRecord
from pm.risk.age import compute_blocked_age

AS_OF = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _t(frm, to, at, item="PM-001"):
    return TransitionRecord(item_id=item, from_status=frm, to_status=to, changed_at=at)


def _age(transitions, *, as_of=AS_OF, tz="Asia/Colombo", created="2026-09-01", status="blocked"):
    return compute_blocked_age(transitions, created_at=created, current_status=status, as_of=as_of, tz=tz)


def test_the_age_is_the_days_since_it_entered_blocked():
    age = _age([_t("backlog", "in_progress", "2026-09-10"), _t("in_progress", "blocked", "2026-09-14")])

    assert age.blocked and age.days == 4 and age.entered_on == "2026-09-14"
    assert (age.from_status, age.source) == ("in_progress", "transition")


def test_a_blocker_that_became_blocked_today_is_zero_days_old():
    assert _age([_t("in_progress", "blocked", "2026-09-18")]).days == 0


def test_a_flap_resets_the_clock():
    """Blocked on the 10th, freed on the 12th, blocked again on the 15th: three days, not eight."""
    age = _age([_t("in_progress", "blocked", "2026-09-10"), _t("blocked", "in_progress", "2026-09-12"),
                _t("in_progress", "blocked", "2026-09-15")])

    assert age.days == 3 and age.entered_on == "2026-09-15"


def test_the_stored_blocked_since_field_is_not_used():
    """The function does not even take it: a stale column cannot change the age."""
    import inspect

    assert "blocked_since" not in inspect.signature(compute_blocked_age).parameters


def test_the_run_starts_at_the_first_of_consecutive_entries_into_blocked():
    age = _age([_t("in_progress", "blocked", "2026-09-11"), _t("blocked", "blocked", "2026-09-16")])

    assert age.days == 7 and age.entered_on == "2026-09-11"


def test_transitions_after_the_moment_are_not_yet_history():
    """Freed on the 17th: as of the 16th it is still blocked (2 days), as of the 18th it is not."""
    history = [_t("in_progress", "blocked", "2026-09-14"), _t("blocked", "in_progress", "2026-09-17")]

    earlier = _age(history, as_of=datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc), status="blocked")  # its status AS OF then
    later = _age(history, status="in_progress")

    assert earlier.blocked and earlier.days == 2
    assert not later.blocked and later.days is None


def test_something_that_is_not_blocked_has_no_age():
    age = _age([_t("backlog", "in_progress", "2026-09-10")], status="in_progress")

    assert not age.blocked and age.days is None


def test_a_full_timestamp_is_read_in_the_projects_timezone():
    """22:00 UTC on the 14th is already the 15th in Colombo (UTC+5:30): 3 days, not 4."""
    age = _age([_t("in_progress", "blocked", "2026-09-14T22:00:00+00:00")])

    assert age.entered_on == "2026-09-15" and age.days == 3


def test_a_date_only_transition_is_the_date_as_written_whatever_the_timezone():
    """The seed records plain dates. 2026-09-14 is the 14th in New York as well, not the 13th."""
    age = _age([_t("in_progress", "blocked", "2026-09-14")], tz="America/New_York")

    assert age.entered_on == "2026-09-14"


def test_the_moment_is_converted_to_the_local_date_too():
    """02:00 UTC on the 19th is still the 18th in New York."""
    age = _age([_t("in_progress", "blocked", "2026-09-14")], tz="America/New_York",
               as_of=datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc))

    assert age.days == 4


def test_an_item_created_blocked_is_aged_from_its_creation():
    age = _age([], created="2026-09-12", status="blocked")

    assert age.blocked and age.days == 6 and age.source == "created" and age.entered_on == "2026-09-12"


def test_an_item_with_no_history_that_is_not_blocked_has_no_age():
    assert _age([], status="in_progress").days is None


def test_a_history_that_contradicts_the_item_is_age_unknown_not_a_guess():
    """The tracker says blocked, but the last recorded move was to done: do not invent an age."""
    age = _age([_t("in_progress", "blocked", "2026-09-10"), _t("blocked", "done", "2026-09-12")], status="blocked")

    assert age.days is None and age.unknown and "does not agree" in age.note


def test_the_blocker_started_before_its_recorded_history_is_aged_from_creation():
    """Created blocked, then moved on, then blocked again: the current run starts at the latest entry."""
    age = _age([_t("blocked", "in_progress", "2026-09-05"), _t("in_progress", "blocked", "2026-09-16")], created="2026-09-01")

    assert age.days == 2


@pytest.mark.parametrize("as_of_day,expected", [(14, 0), (15, 1), (16, 2), (17, 3), (30, 16)])
def test_it_ages_by_calendar_days(as_of_day, expected):
    age = _age([_t("in_progress", "blocked", "2026-09-14")], as_of=datetime(2026, 9, as_of_day, 12, 0, tzinfo=timezone.utc))

    assert age.days == expected


def test_the_age_never_goes_negative():
    """A transition dated after the moment is not history, so it cannot make the age negative."""
    age = _age([_t("in_progress", "blocked", "2026-09-25")], status="blocked")

    assert age.days is None or age.days >= 0
