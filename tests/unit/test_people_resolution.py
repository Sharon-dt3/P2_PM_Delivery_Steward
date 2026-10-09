"""PM-32, the similar-name guard: two people with similar names are never merged, and an ambiguous attribution is said to be ambiguous.

Done when: both similar-named assignees (olivia.dupont, olivia.dupree) keep distinct per-person sections.

The rule (pm.people, shared with P1): a person is their id; a name is a label. A reference resolves to a person only when it is EXACT (an id, an explicit alias,
or one person's exact display name, lower-cased and trimmed, the key P1 matches on). Anything that merely resembles someone is never merged into them: it is
ambiguous when it could be more than one person, and unknown (with the closest match named) when it could be one.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from pm.eval.pm12_cases import ScriptedGateway
from pm.eval.pm23_cases import facts_from_structure, facts_from_text
from pm.people import (
    AMBIGUOUS,
    IGNORED,
    PERSON,
    UNKNOWN,
    Person,
    label_for,
    labels,
    name_key,
    resembles,
    resolve,
)
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import generate_morning_brief
from pm.state.snapshot import build_current_snapshot

ROSTER = [Person("olivia.dupont", "Olivia Dupont"), Person("olivia.dupree", "Olivia Dupree"), Person("wei.chen", "Wei Chen"),
          Person("noah.becker", "Noah Becker"), Person("mateo.silva", "Mateo Silva"), Person("sofia.lindqvist", "Sofia Lindqvist")]
ALIASES = {"wchen@acme.example": "wei.chen", "ghost@acme.example": "nobody.here"}
IGNORED_ACCOUNTS = ["ci-bot", "dependabot[bot]"]
ANCHOR = "2026-09-18T12:00:00+00:00"


def answer(reference, roster=ROSTER):
    return resolve(reference, roster, aliases=ALIASES, ignored=IGNORED_ACCOUNTS)


# --- the rule ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("reference", "person"), [
    ("olivia.dupont", "olivia.dupont"), ("OLIVIA.DUPREE", "olivia.dupree"), ("  olivia.dupree  ", "olivia.dupree"),  # an id, ignoring case and outer spaces
    ("Olivia Dupont", "olivia.dupont"), ("olivia dupree", "olivia.dupree"), (" OLIVIA DUPONT ", "olivia.dupont"),  # one person's exact display name
    ("wchen@acme.example", "wei.chen"), ("WChen@Acme.Example", "wei.chen"),  # an explicit alias
])
def test_an_exact_id_alias_or_display_name_is_that_person_and_never_the_similar_one(reference, person):
    result = answer(reference)

    assert (result.kind, result.person_id) == (PERSON, person)


@pytest.mark.parametrize("reference", ["ci-bot", "CI-Bot", "dependabot[bot]"])
def test_an_ignored_account_is_ignored(reference):
    assert answer(reference).kind == IGNORED


@pytest.mark.parametrize("reference", ["Olivia", "olivia", "olivia d", "Olivia Dup", "O Dup", "Dup", "o. dup", "DUP"])
def test_a_reference_that_could_be_either_olivia_is_ambiguous_and_names_both(reference):
    result = answer(reference)

    assert (result.kind, result.person_id, result.candidates) == (AMBIGUOUS, None, ("olivia.dupont", "olivia.dupree"))


@pytest.mark.parametrize(("reference", "closest"), [
    ("O. Dupont", "olivia.dupont"), ("Dupont Olivia", "olivia.dupont"), ("Olivia Dupnt", "olivia.dupont"), ("Olivia Dupr", "olivia.dupree"),
    ("Wei", "wei.chen"), ("w.chen", "wei.chen"), ("Silva", "mateo.silva"),
])
def test_a_reference_that_resembles_one_person_is_not_merged_into_them_it_is_unknown_with_the_closest_named(reference, closest):
    result = answer(reference)

    assert (result.kind, result.person_id, result.candidates) == (UNKNOWN, None, (closest,))


@pytest.mark.parametrize("reference", ["Oliver Dupont", "x", "", "   ", "Olivia Dupont-Smith", "someone.else@acme.example"])
def test_a_reference_that_matches_nobody_is_unknown_with_no_candidate(reference):
    result = answer(reference)

    assert (result.kind, result.person_id, result.candidates) == (UNKNOWN, None, ())


def test_two_people_with_exactly_the_same_name_are_ambiguous_by_name_but_each_is_exact_by_id():
    twins = [Person("sam.one", "Sam Lee"), Person("sam.two", "sam lee ")]  # the same name by the shared key: case and outer spaces do not tell them apart

    assert answer("Sam Lee", twins).kind == AMBIGUOUS and answer("Sam Lee", twins).candidates == ("sam.one", "sam.two")
    assert answer("Sam Lee", twins).reason == "more than one person has exactly this name"  # said as that, not as a mere resemblance
    assert (answer("sam.one", twins).kind, answer("sam.one", twins).person_id) == (PERSON, "sam.one")
    assert (answer("SAM.TWO", twins).kind, answer("SAM.TWO", twins).person_id) == (PERSON, "sam.two")


def test_a_single_word_is_not_typo_matched_only_aligned_so_a_short_name_does_not_attract_every_near_miss():
    short = [Person("cher", "Cher")]

    assert answer("Cheer", short).candidates == () and answer("Che", short).candidates == ("cher",)  # a prefix lines up; a one-word typo does not


def test_an_alias_for_someone_who_is_not_on_the_roster_resolves_to_nobody():
    result = answer("ghost@acme.example")

    assert result.kind == UNKNOWN and "not on the roster" in result.reason


# --- never merged: the property, over every similar pair --------------------------------------------------------------------------------------------


def variants(person: Person) -> set[str]:
    """Every way of writing someone's name that is NOT their id, their exact display name, or only differs by case or outer spaces."""
    first, _, last = person.name.partition(" ")
    out = {first, last, f"{first[0]}. {last}", f"{first[0]} {last}", f"{last} {first}", f"{first} {last[:3]}", f"{first} {last[:-1]}", last[:3],
           f"{first} {last[:2]}{last[3:]}", f"{first}  {last}x", f"{first[0]}{last}".lower(), f"{first}.{last[:1]}"}
    return {v for v in out if name_key(v) not in {name_key(person.name), name_key(person.id)} and v.strip()}


@pytest.mark.parametrize("person", ROSTER, ids=lambda p: p.id)
def test_no_way_of_writing_a_name_other_than_exactly_it_ever_resolves_to_a_person(person):
    for variant in variants(person):
        assert answer(variant).kind in (AMBIGUOUS, UNKNOWN), f"{variant!r} was merged into {answer(variant).person_id}"


def test_each_of_two_similar_people_resolves_to_themselves_by_every_exact_spelling_and_never_to_the_other():
    for person in ROSTER:
        for spelling in (person.id, person.id.upper(), f" {person.id} ", person.name, person.name.lower(), f"  {person.name.upper()} "):
            assert answer(spelling).person_id == person.id


def test_the_same_reference_always_gets_the_same_answer():
    assert [answer("Olivia") for _ in range(5)] == [answer("Olivia")] * 5


def test_resembles_is_only_a_hint_it_lines_up_words_or_catches_a_typo_and_nothing_more():
    dupont = ROSTER[0]

    assert resembles("Olivia", dupont) and resembles("o dup", dupont) and resembles("Olivia Dupnt", dupont)
    assert not resembles("Oliver", dupont) and not resembles("", dupont) and not resembles("Dupree", dupont)
    assert not resembles("Dupont Dupont", dupont)  # a word cannot line up twice with the same word of theirs


# --- labels for a heading or a sentence -------------------------------------------------------------------------------------------------------------


def test_similar_names_keep_their_plain_names_and_identical_names_get_their_ids():
    twins = [Person("sam.one", "Sam Lee"), Person("sam.two", "SAM LEE"), Person("olivia.dupont", "Olivia Dupont"), Person("olivia.dupree", "Olivia Dupree")]

    assert labels(twins) == {"sam.one": "Sam Lee (sam.one)", "sam.two": "SAM LEE (sam.two)", "olivia.dupont": "Olivia Dupont", "olivia.dupree": "Olivia Dupree"}
    assert label_for("nobody", twins) == "nobody"


# --- the brief: the acceptance test ------------------------------------------------------------------------------------------------------------


def brief(db, taken_at=ANCHOR):
    facts = compute_morning_brief_facts(build_current_snapshot(db, taken_at=taken_at, tz_name="Asia/Colombo"))
    return facts, generate_morning_brief(facts, ScriptedGateway())


def add_commits(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO commits (sha, author_id, message, item_ref, committed_at) VALUES (?, ?, ?, NULL, '2026-09-17')", rows)
    conn.commit()
    conn.close()


def section(content, heading):
    return content.split(f"## {heading}\n")[1].split("\n\n")[0]


def test_both_similar_named_assignees_keep_distinct_per_person_sections(seeded_db_path):
    facts, result = brief(seeded_db_path)

    headings = re.findall(r"^## (.+)$", result.content, flags=re.MULTILINE)
    assert "Olivia Dupont" in headings and "Olivia Dupree" in headings and headings.index("Olivia Dupont") != headings.index("Olivia Dupree")
    dupont, dupree = section(result.content, "Olivia Dupont"), section(result.content, "Olivia Dupree")
    own = {p.assignee_id: {i.item_id for b in (p.delivered, p.pending, p.blocked) for i in b} | {u.item_id for u in p.unmapped} for p in facts.people}
    assert own["olivia.dupont"] and own["olivia.dupree"] and not own["olivia.dupont"] & own["olivia.dupree"]
    for item in own["olivia.dupont"]:
        assert item in dupont and item not in dupree
    for item in own["olivia.dupree"]:
        assert item in dupree and item not in dupont


def test_each_ones_commitments_stay_their_own(seeded_db_path):
    _, result = brief(seeded_db_path)
    dupont, dupree = section(result.content, "Olivia Dupont"), section(result.content, "Olivia Dupree")

    assert "Nightly billing sync SLA fix" in dupont and "Nightly billing sync SLA fix" not in dupree
    assert "staging DB migration" in dupree and "staging DB migration" not in dupont


def test_commits_authored_as_each_exact_id_are_counted_for_that_person_only(seeded_db_path):
    before, _ = brief(seeded_db_path)
    add_commits(seeded_db_path, [("f9a0001", "olivia.dupree", "x"), ("f9a0002", "OLIVIA.DUPREE", "y"), ("f9a0003", "Olivia Dupont", "z")])

    facts, _ = brief(seeded_db_path)
    was = {p.assignee_id: p.commit_count for p in before.people}
    now = {p.assignee_id: p.commit_count for p in facts.people}

    assert (now["olivia.dupree"] - was["olivia.dupree"], now["olivia.dupont"] - was["olivia.dupont"]) == (2, 1)


# --- ambiguous attribution is surfaced as ambiguous ------------------------------------------------------------------------------------------------


def test_an_author_who_could_be_either_olivia_is_credited_to_neither_and_listed_as_ambiguous(seeded_db_path):
    before, _ = brief(seeded_db_path)
    add_commits(seeded_db_path, [("f9b0001", "Olivia", "one"), ("f9b0002", "olivia", "two"), ("f9b0003", "olivia d", "three")])

    facts, result = brief(seeded_db_path)
    counts = {p.assignee_id: p.commit_count for p in facts.people}
    before_counts = {p.assignee_id: p.commit_count for p in before.people}

    assert counts["olivia.dupont"] == before_counts["olivia.dupont"] and counts["olivia.dupree"] == before_counts["olivia.dupree"]  # neither gained one
    assert [(n.kind, n.author, n.commits, n.candidates) for n in facts.author_notes] == [
        ("ambiguous", "Olivia", 2, ["Olivia Dupont", "Olivia Dupree"]), ("ambiguous", "olivia d", 1, ["Olivia Dupont", "Olivia Dupree"])]
    shown = section(result.content, "Authors not matched to a person")
    assert "- 'Olivia' (2 commits): ambiguous, it could be Olivia Dupont or Olivia Dupree. Not counted for either." in shown
    assert "- 'olivia d' (1 commit): ambiguous, it could be Olivia Dupont or Olivia Dupree. Not counted for either." in shown
    assert "## Olivia" not in result.content.replace("## Olivia Dupont", "").replace("## Olivia Dupree", "")  # and no phantom section for 'Olivia'


def test_an_author_who_resembles_one_person_gets_their_own_name_a_note_and_is_not_merged(seeded_db_path):
    before, _ = brief(seeded_db_path)
    add_commits(seeded_db_path, [("f9c0001", "O. Dupont", "one"), ("f9c0002", "Olivia Dupnt", "two")])

    facts, result = brief(seeded_db_path)
    counts = {p.assignee_id: p.commit_count for p in facts.people}

    assert counts["olivia.dupont"] == {p.assignee_id: p.commit_count for p in before.people}["olivia.dupont"]  # not credited to Dupont
    assert counts["O. Dupont"] == counts["Olivia Dupnt"] == 1  # each is its own name, never someone else's
    shown = section(result.content, "Authors not matched to a person")
    assert ("- 'O. Dupont' (1 commit): not matched to anyone. It resembles Olivia Dupont but is not their id, name or an alias, so it is not merged into them.") in shown


def test_the_ambiguous_author_is_marked_on_the_commit_that_has_no_item_reference(seeded_db_path):
    add_commits(seeded_db_path, [("f9d0001", "Olivia", "tidy up the build")])

    facts, result = brief(seeded_db_path)

    assert any(c.sha == "f9d0001" and c.author == "Olivia (ambiguous: could be Olivia Dupont or Olivia Dupree)" for c in facts.unreferenced_commits)
    assert "f9d0001: tidy up the build (Olivia (ambiguous: could be Olivia Dupont or Olivia Dupree)," in result.content


def test_a_brief_with_nothing_ambiguous_has_no_such_section(seeded_db_path):
    _, result = brief(seeded_db_path)

    assert "Authors not matched to a person" not in result.content


def test_the_note_is_a_checked_fact_so_a_dropped_or_changed_one_is_caught(seeded_db_path):
    add_commits(seeded_db_path, [("f9e0001", "Olivia", "one")])
    facts, result = brief(seeded_db_path)
    expected = facts_from_structure(facts)
    line = "- 'Olivia' (1 commit): ambiguous, it could be Olivia Dupont or Olivia Dupree. Not counted for either."

    assert ("author_note", "ambiguous", "Olivia", 1) in expected and facts_from_text(result.content) == expected
    assert facts_from_text(result.content.replace(line, "")) != expected
    assert facts_from_text(result.content.replace("(1 commit)", "(2 commits)")) != expected


# --- identical names, and the channel brief --------------------------------------------------------------------------------------------------------------


def test_two_assignees_with_the_same_name_get_separate_sections_told_apart_by_id(seeded_db_path):
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE assignees SET display_name = 'Olivia Dupont' WHERE id = 'olivia.dupree'")
    conn.commit()
    conn.close()

    _, result = brief(seeded_db_path)

    headings = re.findall(r"^## (.+)$", result.content, flags=re.MULTILINE)
    assert "Olivia Dupont (olivia.dupont)" in headings and "Olivia Dupont (olivia.dupree)" in headings and "Olivia Dupont" not in headings


def test_assignees_whose_names_differ_only_in_case_are_still_told_apart_by_id(seeded_db_path):
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE assignees SET display_name = 'OLIVIA DUPONT ' WHERE id = 'olivia.dupree'")
    conn.commit()
    conn.close()

    _, result = brief(seeded_db_path)

    headings = re.findall(r"^## (.+)$", result.content, flags=re.MULTILINE)
    assert "Olivia Dupont (olivia.dupont)" in headings and "OLIVIA DUPONT  (olivia.dupree)" in headings  # the same name by the shared key, so neither stands alone


def p1_store(tmp_path, members):
    from pm.channelbrief.facts import P1Directory

    path = tmp_path / "p1.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE members (id TEXT PRIMARY KEY, display_name TEXT); CREATE TABLE messages (id TEXT PRIMARY KEY, author_id TEXT, posted_at TEXT);")
    conn.executemany("INSERT INTO members VALUES (?, ?)", members)
    conn.commit()
    conn.close()
    return P1Directory(path)


def test_in_the_channel_brief_similar_names_stay_plain_and_identical_names_are_told_apart(tmp_path):
    directory = p1_store(tmp_path, [("aad-0001-dupont", "Olivia Dupont"), ("aad-0002-dupree", "Olivia Dupree"),
                                    ("aad-0003-samone", "Sam Lee"), ("aad-0004-samtwo", "sam lee ")])

    assert directory.name("aad-0001-dupont") == "Olivia Dupont" and directory.name("aad-0002-dupree") == "Olivia Dupree"
    assert directory.name("aad-0003-samone") == "Sam Lee (aad-0003)" and directory.name("aad-0004-samtwo") == "sam lee (aad-0004)"
    assert directory.name("unknown") is None


def test_a_placeholder_name_does_not_make_two_real_names_look_identical(tmp_path):
    directory = p1_store(tmp_path, [("aad-new", "aad-new"), ("aad-0001-dupont", "Olivia Dupont")])

    assert directory.name("aad-new") is None and directory.name("aad-0001-dupont") == "Olivia Dupont"


# --- one rule with P1 ---------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("ours", "theirs"), [
    ("Olivia Dupont", "olivia dupont"), ("Olivia Dupont", "  OLIVIA DUPONT "), ("Olivia Dupont", "Olivia Dupree"), ("Olivia Dupont", "Olivia  Dupont"),
    ("Olivia Dupont", "O. Dupont"), ("Wei Chen", "wei chen"), ("Sam Lee", "Sam Lee Jr"), ("", ""),
])
def test_the_key_is_exactly_what_p1_matches_on_lower_of_trim(ours, theirs):
    conn = sqlite3.connect(":memory:")
    sql_equal = conn.execute("SELECT lower(trim(?)) = lower(trim(?))", (ours, theirs)).fetchone()[0]

    assert bool(sql_equal) == (name_key(ours) == name_key(theirs))


def test_p1s_shared_cap_and_p2s_match_names_with_the_same_expression():
    expression = "lower(trim(display_name)) = lower(trim(?))"
    p2 = (Path(__file__).resolve().parents[2] / "src" / "pm" / "commitments" / "cap.py").read_text(encoding="utf-8")
    p1_path = Path(__file__).resolve().parents[3] / "P3_Agents" / "src" / "p1" / "nudges" / "shared_cap.py"
    if not p1_path.exists():
        pytest.skip("P1's repository is not next to this one")

    assert expression in p2 and expression in p1_path.read_text(encoding="utf-8")  # if either side changes how it matches names, this says so
