"""Who a commit belongs to. The tracker and the code host are separate systems
and need not spell a person the same way, and a code host also holds automated
accounts. An IdentityMap (aliases -> canonical id, plus ignored automated
authors) is carried on the snapshot and applied when facts count commits, so:
one person is one entry in the brief, and a bot is not a teammate.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from pm.adapters.code_host import Commit
from pm.adapters.tracker import Assignee
from pm.eval.pm12_cases import ScriptedGateway, _person_block, count_fabrications
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import generate_morning_brief
from pm.seed.build import ANCHOR_DATE
from pm.state.identities import IdentityError, IdentityMap, load_identities
from pm.state.snapshot import ChannelSnapshot, ProjectSnapshot, build_current_snapshot
from pm.storage.db import get_connection

ROSTER = ["wei.chen", "noah.becker"]


# --- the map itself ---------------------------------------------------------


def test_an_unknown_author_resolves_to_itself():
    assert IdentityMap().resolve("someone.new", ROSTER) == "someone.new"


def test_an_alias_resolves_to_its_canonical_id():
    identities = IdentityMap(aliases={"wchen@acme.example": "wei.chen"})

    assert identities.resolve("wchen@acme.example", ROSTER) == "wei.chen"


def test_matching_ignores_case_and_surrounding_spaces():
    identities = IdentityMap(aliases={"WChen@Acme.Example": "wei.chen"}, ignored_authors=["CI-Bot"])

    assert identities.resolve("  wchen@acme.EXAMPLE ", ROSTER) == "wei.chen"
    assert identities.resolve("Wei.Chen", ROSTER) == "wei.chen"  # roster id, different case
    assert identities.resolve("ci-BOT", ROSTER) is None


def test_an_ignored_author_resolves_to_none():
    assert IdentityMap(ignored_authors=["dependabot[bot]"]).resolve("dependabot[bot]", ROSTER) is None


def test_an_alias_may_point_at_someone_not_on_the_roster_but_is_never_guessed():
    identities = IdentityMap(aliases={"kofi@acme.example": "kofi.mensah"})

    assert identities.resolve("kofi@acme.example", ROSTER) == "kofi.mensah"
    assert identities.resolve("kofi", ROSTER) == "kofi"  # no fuzzy matching of names


def test_an_alias_that_is_another_persons_roster_id_is_rejected():
    with pytest.raises(IdentityError, match="noah.becker"):
        IdentityMap(aliases={"noah.becker": "wei.chen"}).validate(ROSTER)


def test_an_alias_that_is_also_ignored_is_rejected():
    with pytest.raises(IdentityError, match="ci-bot"):
        IdentityMap(aliases={"ci-bot": "wei.chen"}, ignored_authors=["ci-bot"]).validate(ROSTER)


def test_two_aliases_that_differ_only_by_case_but_disagree_are_rejected():
    with pytest.raises(IdentityError):
        IdentityMap(aliases={"A@x": "wei.chen", "a@x": "noah.becker"}).validate(ROSTER)


def test_an_ignored_roster_member_is_rejected():
    with pytest.raises(IdentityError, match="wei.chen"):
        IdentityMap(ignored_authors=["wei.chen"]).validate(ROSTER)


def test_a_missing_file_is_an_empty_map(tmp_path):
    assert load_identities(tmp_path / "nope.json") == IdentityMap()


def test_a_file_is_loaded(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"aliases": {"w@x": "wei.chen"}, "ignored_authors": ["ci-bot"]}))

    assert load_identities(path) == IdentityMap(aliases={"w@x": "wei.chen"}, ignored_authors=["ci-bot"])


@pytest.mark.parametrize("content", ["not json", '["a list"]', '{"aliases": {"a": 1}}', '{"ignored_authors": "bot"}'])
def test_a_malformed_file_fails_loudly_not_silently(tmp_path, content):
    path = tmp_path / "identities.json"
    path.write_text(content)

    with pytest.raises(IdentityError, match="identities.json"):
        load_identities(path)


# --- facts ------------------------------------------------------------------


def _commit(sha, author):
    return Commit(sha=sha, author_id=author, message="x", committed_at="2026-09-15T10:00:00+00:00")


def _snapshot(commits, identities=None):
    return ProjectSnapshot(
        taken_at="2026-09-16T23:59:59+00:00", items=[], commits=commits,
        channel=ChannelSnapshot(channel_id="c", messages=[]),
        roster=[Assignee(id=i, display_name=i) for i in ROSTER],
        **({"identities": identities} if identities else {}),
    )


def _people(snapshot):
    return {p.assignee_id: p for p in compute_morning_brief_facts(snapshot).people}


def test_without_a_map_the_same_person_under_two_ids_is_two_entries():
    people = _people(_snapshot([_commit("1", "wei.chen"), _commit("2", "wchen@acme.example")]))

    assert set(people) == {"wei.chen", "noah.becker", "wchen@acme.example"}


def test_an_aliased_author_is_counted_under_the_one_canonical_person():
    identities = IdentityMap(aliases={"wchen@acme.example": "wei.chen"})
    people = _people(_snapshot([_commit("1", "wei.chen"), _commit("2", "wchen@acme.example")], identities))

    assert set(people) == {"wei.chen", "noah.becker"}
    assert people["wei.chen"].commit_count == 2


def test_a_bot_is_not_a_person_and_its_commits_are_counted_separately():
    identities = IdentityMap(ignored_authors=["ci-bot"])
    snapshot = _snapshot([_commit("1", "ci-bot"), _commit("2", "ci-bot"), _commit("3", "wei.chen")], identities)
    facts = compute_morning_brief_facts(snapshot)

    assert "ci-bot" not in {p.assignee_id for p in facts.people}
    assert facts.automated_commit_count == 2
    assert next(p for p in facts.people if p.assignee_id == "wei.chen").commit_count == 1


def test_a_snapshot_carries_an_invalid_map_no_further_than_its_own_validation():
    with pytest.raises(IdentityError):
        compute_morning_brief_facts(_snapshot([], IdentityMap(ignored_authors=["wei.chen"])))


# --- end to end, on the real seeded database with extra people overlaid -------
# The canonical seed stays at 7 people (the plan caps it at 5-7) and keeps
# sofia.lindqvist as its one silent member, so these cases are layered onto a
# copy of it: a commit-only teammate, an aliased author and a bot.


def _overlay(db_path):
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")  # the code host's authors need not exist in the tracker
        conn.execute("INSERT INTO assignees (id, display_name) VALUES ('kofi.mensah', 'Kofi Mensah')")
        rows = [
            ("k000001", "kofi.mensah", "chore: tidy build scripts"),
            ("k000002", "kofi.mensah", "docs: update runbook"),
            ("k000003", "wchen@acme.example", "chore: lint"),
            ("k000004", "ci-bot", "ci: nightly cache refresh"),
            ("k000005", "ci-bot", "ci: dependency bump"),
        ]
        for sha, author, message in rows:
            conn.execute(
                "INSERT INTO commits (sha, author_id, message, item_ref, committed_at) VALUES (?, ?, ?, NULL, '2026-09-15')",
                (sha, author, message),
            )
        conn.commit()
    finally:
        conn.close()


IDENTITIES = IdentityMap(aliases={"wchen@acme.example": "wei.chen"}, ignored_authors=["ci-bot"])


def _seeded_snapshot(seeded_db_path, identities):
    _overlay(seeded_db_path)
    return build_current_snapshot(seeded_db_path, taken_at=f"{ANCHOR_DATE}T23:59:59+00:00", identities=identities)


def test_on_the_seed_a_commit_only_teammate_shows_their_commits(seeded_db_path):
    facts = compute_morning_brief_facts(_seeded_snapshot(seeded_db_path, IDENTITIES))
    brief = generate_morning_brief(facts, ScriptedGateway())

    assert _person_block(brief.content, "kofi.mensah") == ["- Commits: 2 recorded; no tracker items."]
    assert count_fabrications(brief, facts) == []


def test_on_the_seed_the_alias_adds_to_wei_and_creates_no_extra_person(seeded_db_path):
    without = compute_morning_brief_facts(_seeded_snapshot(seeded_db_path, IdentityMap()))
    with_map = compute_morning_brief_facts(build_current_snapshot(
        seeded_db_path, taken_at=f"{ANCHOR_DATE}T23:59:59+00:00", identities=IDENTITIES))

    def wei(facts):
        return next(p for p in facts.people if p.assignee_id == "wei.chen").commit_count

    assert "wchen@acme.example" in {p.assignee_id for p in without.people}
    assert "wchen@acme.example" not in {p.assignee_id for p in with_map.people}
    assert wei(with_map) == wei(without) + 1


def test_on_the_seed_the_bot_is_left_out_but_counted(seeded_db_path):
    facts = compute_morning_brief_facts(_seeded_snapshot(seeded_db_path, IDENTITIES))
    brief = generate_morning_brief(facts, ScriptedGateway())

    assert "ci-bot" not in brief.content
    assert facts.automated_commit_count == 2


def test_sofia_is_still_the_silent_one_after_the_overlay(seeded_db_path):
    facts = compute_morning_brief_facts(_seeded_snapshot(seeded_db_path, IDENTITIES))

    assert [p.assignee_id for p in facts.people if not p.has_activity] == ["sofia.lindqvist"]


def test_the_overlay_database_really_is_the_seed_plus_the_extras(seeded_db_path):
    _overlay(seeded_db_path)
    conn = sqlite3.connect(seeded_db_path)
    try:
        (n,) = conn.execute("SELECT COUNT(*) FROM assignees").fetchone()
    finally:
        conn.close()

    assert n == 8
