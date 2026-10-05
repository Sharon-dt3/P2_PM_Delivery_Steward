"""PM-12: golden cases 1 (citation rate) and 2 (fabricated-claim count).

The row's own acceptance test is "both metrics printed and committed";
these tests prove the numbers mean something: GC1 is a real measurement
(planted slips are really counted), and GC2's probe is not vacuous -- it
demonstrably flags a fabrication when handed one, so a zero from it is
evidence rather than an artefact of a check that cannot fail.
"""

from __future__ import annotations

from spine.eval.cases import GoldenCaseRegistry
from spine.grounding.kernel import FactualLine

from pm.eval.pm12_cases import (
    GC1_TARGET,
    ScriptedGateway,
    _cross_section_tamper,
    _empty_day_facts,
    build_seeded_facts,
    count_fabrications,
    measure_gc1,
    measure_gc2,
)
from pm.eval.registrations import register_all
from pm.reporting.morning_brief import generate_morning_brief


def test_register_all_adds_gc1_and_gc2(seeded_db_path):
    registry = GoldenCaseRegistry()
    register_all(registry, db_path=seeded_db_path)
    assert {"GC1", "GC2"} <= {case.case_id for case in registry.all_cases()}  # later rows (GC6, PM-14) add theirs
    for case in registry.all_cases():
        assert case.measure_fn()  # runs end to end through the registry


def test_gc1_meets_its_target_and_counts_the_planted_slips(seeded_db_path):
    facts = build_seeded_facts(seeded_db_path)
    (result,) = measure_gc1(facts)

    assert result.comparator_name == "at_least"
    assert result.target == GC1_TARGET == 0.90
    assert result.passed is True
    # Not a vacuous 1.0: the two hand-planted first-attempt slips (one line
    # with no reference, one citing a nonexistent item) are really counted.
    assert result.measured < 1.0
    assert "resolve" in result.detail


def test_gc2_is_a_hard_zero_on_the_real_seeded_brief(seeded_db_path):
    facts = build_seeded_facts(seeded_db_path)
    (result,) = measure_gc2(facts)

    assert result.comparator_name == "at_most"
    assert result.target == 0
    assert result.measured == 0, result.detail
    assert result.passed is True


def test_the_seed_really_contains_a_zero_activity_person(seeded_db_path):
    """GC2's zero-activity check is only meaningful if the seeded facts
    contain someone with no activity. A stale data/pm.db built before
    PM-10 once silently dropped her; tests build their own fresh seed, and
    this pins that the case being probed is real."""
    facts = build_seeded_facts(seeded_db_path)
    assert [p.assignee_id for p in facts.people if not p.has_activity] == ["sofia.lindqvist"]


def test_the_fabrication_probe_flags_a_cross_section_claim_when_handed_one(seeded_db_path):
    facts = build_seeded_facts(seeded_db_path)
    brief = generate_morning_brief(facts, ScriptedGateway(tamper=_cross_section_tamper(facts)))
    assert count_fabrications(brief, facts) == []

    # Hand-forge the exact fabrication the fix prevents: a real pending
    # item's reference smuggled into the delivered section.
    foreign = next(iter(facts.people[0].pending), None) or next(
        item for person in facts.people for item in person.pending
    )
    forged = FactualLine(text="Someone delivered it.", message_id=f"item:{foreign.item_id}")
    brief.sections["delivered"].append(forged)

    problems = count_fabrications(brief, facts)
    assert any("delivered" in problem and foreign.item_id in problem for problem in problems)


def test_the_fabrication_probe_flags_a_zero_activity_person_given_invented_activity(seeded_db_path):
    facts = build_seeded_facts(seeded_db_path)
    brief = generate_morning_brief(facts, ScriptedGateway())
    assert count_fabrications(brief, facts) == []

    brief.content = brief.content.replace(
        "- No update: no tracker activity or commits recorded.", "- Delivered: Sofia shipped the search index."
    )
    assert any("sofia.lindqvist" in problem for problem in count_fabrications(brief, facts))


def test_an_empty_day_never_calls_the_model_and_reports_nothing_made_up():
    facts = _empty_day_facts()
    gateway = ScriptedGateway()
    brief = generate_morning_brief(facts, gateway)

    assert gateway.calls == 0
    assert count_fabrications(brief, facts) == []
    assert "No update: no tracker activity or commits recorded." in brief.content
