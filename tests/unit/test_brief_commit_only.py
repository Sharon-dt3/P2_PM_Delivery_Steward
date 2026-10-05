"""PM-10, second half: a person whose only activity is commits (nothing in the
tracker) used to render four "none." lines that hid the commits. They now get
one fixed line stating the commit count and that there are no tracker items.
The count is a fact computed in code; the line is a template, never model prose.
"""

from __future__ import annotations

from pm.eval.pm12_cases import ScriptedGateway, _person_block, count_fabrications
from pm.reporting.facts import ItemFact, MorningBriefFacts, PersonFacts
from pm.reporting.morning_brief import generate_morning_brief


def _person(assignee_id="dev.one", commit_count=0, delivered=()):
    return PersonFacts(
        assignee_id=assignee_id, committed=[], delivered=list(delivered), pending=[], blocked=[],
        commit_count=commit_count,
    )


def _facts(*people):
    return MorningBriefFacts(as_of="2026-09-15T23:59:59+00:00", sprint=None, people=list(people), blockers=[])


def _block(facts, assignee="dev.one"):
    gateway = ScriptedGateway()
    brief = generate_morning_brief(facts, gateway)
    return brief, gateway, _person_block(brief.content, assignee)


def test_a_commit_only_person_shows_their_commit_count_not_four_nones():
    _, _, block = _block(_facts(_person(commit_count=3)))

    assert block == ["- Commits: 3 recorded; no tracker items."]
    assert "none." not in "\n".join(block)


def test_one_commit_is_singular():
    _, _, block = _block(_facts(_person(commit_count=1)))

    assert block == ["- Commits: 1 recorded; no tracker items."]


def test_no_model_call_is_made_for_a_commit_only_person():
    _, gateway, _ = _block(_facts(_person(commit_count=3)))

    assert gateway.calls == 0


def test_a_person_with_no_commits_and_no_items_still_gets_the_no_update_line():
    _, _, block = _block(_facts(_person(commit_count=0)))

    assert block == ["- No update: no tracker activity or commits recorded."]


def test_a_person_with_tracker_items_keeps_the_four_buckets():
    delivered = [ItemFact(item_id="PM-001", title="Ship login page")]
    _, _, block = _block(_facts(_person(commit_count=2, delivered=delivered)))

    assert block[0].startswith("- Committed: ")
    assert not any(line.startswith("- Commits:") for line in block)


def test_the_fabrication_probe_flags_a_commit_only_person_rendered_as_nones():
    facts = _facts(_person(commit_count=3))
    brief, _, _ = _block(facts)
    assert count_fabrications(brief, facts) == []  # the real rendering is clean

    hidden = brief.model_copy(update={
        "content": brief.content.replace(
            "- Commits: 3 recorded; no tracker items.",
            "- Committed: none.\n- Delivered: none.\n- Pending: none.\n- Blocked: none.",
        )
    })
    assert any("dev.one" in problem for problem in count_fabrications(hidden, facts))


def test_the_probe_also_flags_a_wrong_commit_count():
    facts = _facts(_person(commit_count=3))
    brief, _, _ = _block(facts)
    wrong = brief.model_copy(update={"content": brief.content.replace("3 recorded", "5 recorded")})

    assert any("dev.one" in problem for problem in count_fabrications(wrong, facts))


def test_commit_only_and_silent_people_render_side_by_side():
    facts = _facts(_person("dev.one", commit_count=4), _person("dev.two", commit_count=0))
    brief, _, _ = _block(facts)

    assert _person_block(brief.content, "dev.one") == ["- Commits: 4 recorded; no tracker items."]
    assert _person_block(brief.content, "dev.two") == ["- No update: no tracker activity or commits recorded."]
