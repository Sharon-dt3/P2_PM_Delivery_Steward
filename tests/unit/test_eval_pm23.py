"""PM-23, golden case 9: determinism of facts.

The row: generate the morning brief twice from the same snapshot; the wording may differ, the set of items,
owners, counts and statuses must not. Acceptance: the fact-set comparison across the two generations prints
zero divergences.

A clean sheet only means something if the comparison can fail, so most of this file perturbs the pipeline
(a facts step that is not deterministic, a snapshot that changes when stored, an item that changes status
between generations, identical wording) and checks the right number notices.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval

from pm.eval import pm23_cases
from pm.eval.pm23_cases import (
    MOMENTS,
    WordedGateway,
    divergences,
    facts_from_structure,
    facts_from_text,
    format_report,
    measure_gc9,
    metrics,
    run_all,
    run_moment,
    scripted_pair,
)
from pm.eval.pristine import build_pristine_database
from pm.eval.registrations import register_all
from pm.reporting.morning_brief import MorningBrief

REPO = Path(__file__).resolve().parents[2]


def _by_id(results):
    return {r.metric_id: r for r in results}


def _runs(tmp_path, **kwargs):
    return run_all(tmp_path, **kwargs)


# --- the acceptance test: the comparison prints zero divergences ---------------------------------------------------------------


def test_the_fact_set_comparison_across_two_generations_prints_zero_divergences(tmp_path, capsys):
    runs = _runs(tmp_path)
    print(format_report(runs))

    printed = capsys.readouterr().out
    assert [len(run.divergences) for run in runs] == [0, 0]
    assert "0 divergences" in printed and "total divergences across the two generations: 0" in printed
    assert all(run.first == run.second for run in runs)  # not merely "no differences reported": the sets are equal


def test_all_the_numbers_pass():
    results = _by_id(measure_gc9())

    for metric in ("GC9-fact-divergence-count", "GC9-snapshot-fact-gap-count", "GC9-snapshot-reload-change-count"):
        assert results[metric].measured == 0 and results[metric].passed, (metric, results[metric].detail)
    assert results["GC9-wording-difference-count"].passed and results["GC9-wording-difference-count"].measured >= 1


def test_the_two_generations_really_are_worded_differently(tmp_path):
    runs = _runs(tmp_path)

    assert all(run.lines_worded_differently == run.lines > 0 for run in runs)  # every line, so zero divergence is not two copies


def test_the_two_moments_are_different_briefs(tmp_path):
    """On the 16th PM-015 is pending (sprint day 10); by the 18th it is blocked (day 12): an item that changes status."""
    first, second = _runs(tmp_path)

    assert first.first != second.first
    assert ("item", "Wei Chen", "Pending", "PM-015") in first.first and ("item", "Wei Chen", "Blocked", "PM-015") in second.first
    assert ("sprint", "day", 10, 14) in first.first and ("sprint", "day", 12, 14) in second.first


def test_the_fact_set_is_substantial(tmp_path):
    runs = _runs(tmp_path)

    kinds = {fact[0] for run in runs for fact in run.first}
    assert {"sprint", "owner", "item", "count", "due", "blocker", "blocker_severity", "no_update"} <= kinds
    assert all(len(run.first) > 60 for run in runs)


def test_each_generation_also_matches_the_facts_computed_from_the_snapshot(tmp_path):
    for run in _runs(tmp_path):
        assert run.first == run.expected and run.second == run.expected_second and run.snapshot_gaps == []


def test_it_is_registered_and_the_numbers_are_recorded(tmp_path):
    registry = GoldenCaseRegistry()
    register_all(registry)
    assert "GC9" in {case.case_id for case in registry.all_cases()}
    results_path = tmp_path / "results.jsonl"

    summary = run_eval(registry, model_id="scripted", results_path=results_path)

    ids = {r.metric_id for r in summary.results}
    assert {"GC9-fact-divergence-count", "GC9-snapshot-fact-gap-count", "GC9-snapshot-reload-change-count",
            "GC9-wording-difference-count"} <= ids and summary.all_passed
    recorded = {r["metric_id"] for line in results_path.read_text().splitlines() for r in json.loads(line)["results"]}
    assert "GC9-fact-divergence-count" in recorded


def test_the_eval_script_prints_the_comparison(monkeypatch, tmp_path, capfd):
    spec = importlib.util.spec_from_file_location("run_eval_script_23", REPO / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RESULTS_PATH", tmp_path / "results.jsonl")
    monkeypatch.setattr("sys.argv", ["run_eval.py"])

    code = module.main()

    out = capfd.readouterr().out
    assert code == 0 and "Golden case 9" in out and "total divergences across the two generations: 0" in out
    assert "GC9-fact-divergence-count" in out


# --- the reader's facts, read out of the text -----------------------------------------------------------------------------------

BRIEF = """Sprint 13 (sprint-13), day 12 of 14 (2026-09-07 to 2026-09-20); 3 of 17 items in this sprint are done.

## Aisha Rahman
- Committed: none.
- Delivered: Aisha Rahman: PM-009 (Write tests). Aisha Rahman: PM-011 (Docs).
- Pending: Aisha Rahman: PM-017 (Copy review).
- Blocked: none.

## Wei Chen
- Committed: Wei Chen committed: "Root-cause it." (due 2026-09-15). Wei Chen committed: "Filter persistence." (due 2026-09-15).
- Delivered: none.
- Pending: none.
- Blocked: Wei Chen: PM-015 (Null case). Wei Chen: PM-023 (Token refresh).

## Sofia Lindqvist
- No update: no tracker activity or commits recorded.

## Kai Moreau
- Commits: 4 recorded; no tracker items.

## Blockers
- [high] RISK-002: Billing at risk (item PM-024, assignee Olivia Dupont).
- [medium] RISK-001: Auth flow failing (item PM-023, assignee Wei Chen)."""


def test_the_parser_reads_every_kind_of_fact():
    facts = facts_from_text(BRIEF)

    assert {("sprint", "id", "sprint-13"), ("sprint", "day", 12, 14), ("sprint", "done", 3, 17)} <= facts
    assert {("owner", "Aisha Rahman"), ("owner", "Wei Chen"), ("owner", "Sofia Lindqvist"), ("owner", "Kai Moreau")} <= facts
    assert {("item", "Aisha Rahman", "Delivered", "PM-009"), ("item", "Aisha Rahman", "Delivered", "PM-011"),
            ("item", "Aisha Rahman", "Pending", "PM-017"), ("item", "Wei Chen", "Blocked", "PM-015"), ("item", "Wei Chen", "Blocked", "PM-023")} <= facts
    assert {("count", "Aisha Rahman", "Delivered", 2), ("count", "Aisha Rahman", "Pending", 1), ("count", "Aisha Rahman", "Blocked", 0),
            ("count", "Wei Chen", "Blocked", 2)} <= facts
    assert ("due", "Wei Chen", "2026-09-15") in facts  # a commitment's due date, wherever the line puts it
    assert ("no_update", "Sofia Lindqvist") in facts and ("commits_only", "Kai Moreau", 4) in facts
    assert {("blocker", 1, "RISK-002"), ("blocker", 2, "RISK-001"), ("blocker_severity", "RISK-002", "high")} <= facts


def test_the_parser_ignores_the_wording():
    reworded = (BRIEF.replace("Aisha Rahman: PM-009 (Write tests)", "Today, Aisha Rahman: PM-009 (Write tests) [as recorded]")
                .replace("Billing at risk", "A completely different phrasing of it"))

    assert facts_from_text(reworded) == facts_from_text(BRIEF)


def test_the_parser_reads_the_facts_out_of_a_model_s_natural_phrasing():
    """Found on a real model: dates written inline, ids kept, the sprint line rephrased. The facts are the same."""
    natural = """Sprint 13 is on day 12 of 14, with 3 of 17 items done.

## Wei Chen
- Committed: Wei Chen committed to having the staging auth flow failures root-caused by 2026-09-15. Wei Chen committed to filter persistence landing by 2026-09-19.
- Delivered: Wei Chen delivered PM-002: Implement caching layer eviction policy.
- Pending: Wei Chen has PM-020 (Add rate limiting) pending.
- Blocked: Wei Chen is blocked on PM-015: Null case.

## Blockers
- [high] RISK-002: Billing sync nightly job is at risk of missing SLA.
- [medium] RISK-001: Auth flow token refresh has intermittent failures in staging."""

    facts = facts_from_text(natural)

    assert {("sprint", "id", "sprint-13"), ("sprint", "day", 12, 14), ("sprint", "done", 3, 17)} <= facts
    assert {("due", "Wei Chen", "2026-09-15"), ("due", "Wei Chen", "2026-09-19")} <= facts
    assert {("item", "Wei Chen", "Delivered", "PM-002"), ("item", "Wei Chen", "Pending", "PM-020"), ("item", "Wei Chen", "Blocked", "PM-015")} <= facts
    assert {("blocker", 1, "RISK-002"), ("blocker_severity", "RISK-002", "high"), ("blocker", 2, "RISK-001")} <= facts


def test_the_parser_reads_a_brief_with_no_sprint_and_no_blockers():
    facts = facts_from_text("Sprint scope: no sprint on file covers this date.\n\n## Sofia\n- No update: x.\n\n## Blockers\n- none.")

    assert ("sprint", "none") in facts and ("blockers", "none") in facts and ("no_update", "Sofia") in facts


@pytest.mark.parametrize("change,fragment", [
    (lambda t: t.replace("## Wei Chen", "## Wei Chan"), "owner"),
    (lambda t: t.replace("Aisha Rahman: PM-017 (Copy review)", "Aisha Rahman: PM-018 (Copy review)"), "item Aisha Rahman Pending"),
    (lambda t: t.replace("- Delivered: Aisha Rahman: PM-009 (Write tests). Aisha Rahman: PM-011 (Docs).", "- Delivered: Aisha Rahman: PM-009 (Write tests)."), "count Aisha Rahman Delivered"),
    (lambda t: t.replace("- Pending: Aisha Rahman: PM-017 (Copy review).", "- Pending: none."), "item Aisha Rahman Pending PM-017"),
    (lambda t: t.replace("day 12 of 14", "day 13 of 14"), "sprint day"),
    (lambda t: t.replace("3 of 17 items", "4 of 17 items"), "sprint done"),
    (lambda t: t.replace('(due 2026-09-15). Wei Chen committed: "Filter', '(due 2026-09-16). Wei Chen committed: "Filter'), "due Wei Chen"),
    (lambda t: t.replace("[high] RISK-002", "[low] RISK-002"), "blocker_severity RISK-002"),
    (lambda t: t.replace("Commits: 4 recorded", "Commits: 5 recorded"), "commits_only Kai Moreau"),
    (lambda t: re.sub(r"## Sofia Lindqvist\n- No update: [^\n]*", "## Sofia Lindqvist\n- Commits: 1 recorded; no tracker items.", t), "no_update Sofia"),
], ids=["an owner renamed", "an item swapped", "a count changed", "an item dropped", "the sprint day", "the sprint done-count",
        "a due date", "a severity", "a commit count", "a person's activity"])
def test_every_kind_of_fact_is_compared(change, fragment):
    found = divergences(facts_from_text(BRIEF), facts_from_text(change(BRIEF)))

    assert found and any(fragment in d for d in found), found


def test_a_blocker_out_of_order_is_a_divergence():
    swapped = BRIEF.replace("- [high] RISK-002: Billing at risk (item PM-024, assignee Olivia Dupont).\n- [medium] RISK-001: Auth flow failing (item PM-023, assignee Wei Chen).",
                            "- [medium] RISK-001: Auth flow failing (item PM-023, assignee Wei Chen).\n- [high] RISK-002: Billing at risk (item PM-024, assignee Olivia Dupont).")

    found = divergences(facts_from_text(BRIEF), facts_from_text(swapped))

    assert any("blocker 1" in d for d in found)  # the ranking is a fact: what is first matters


def test_an_item_that_changes_status_is_a_divergence_in_both_places():
    moved = BRIEF.replace("- Pending: Aisha Rahman: PM-017 (Copy review).", "- Pending: none.").replace(
        "- Blocked: none.\n\n## Wei", "- Blocked: Aisha Rahman: PM-017 (Copy review).\n\n## Wei")

    found = divergences(facts_from_text(BRIEF), facts_from_text(moved))

    assert any("only in generation 1: item Aisha Rahman Pending PM-017" in d for d in found)
    assert any("only in generation 2: item Aisha Rahman Blocked PM-017" in d for d in found)


def test_identical_facts_have_no_divergence():
    assert divergences(facts_from_text(BRIEF), facts_from_text(BRIEF)) == []


# --- the numbers can fail: perturb the pipeline ---------------------------------------------------------------------------------


def _first_delivered(facts):
    return next(p for p in facts.people if p.delivered)


def test_a_facts_step_that_is_not_deterministic_is_caught(tmp_path, monkeypatch):
    """The second call drops someone's delivered item, as an unstable ordering or a race might."""
    real, calls = pm23_cases.compute_morning_brief_facts, {"n": 0}

    def flaky(snapshot):
        facts = real(snapshot)
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            person = _first_delivered(facts)
            person.delivered = person.delivered[1:]
        return facts

    monkeypatch.setattr(pm23_cases, "compute_morning_brief_facts", flaky)

    runs = _runs(tmp_path)
    results = _by_id(metrics(runs))

    assert results["GC9-fact-divergence-count"].measured > 0 and not results["GC9-fact-divergence-count"].passed
    assert any("only in generation 1: item" in d for run in runs for d in run.divergences)


def test_a_snapshot_that_changes_when_it_is_stored_is_caught(tmp_path):
    def drop_an_item(snapshot):
        return snapshot.model_copy(update={"items": [i for i in snapshot.items if i.id != "PM-009"]})

    db = build_pristine_database(tmp_path)
    run = run_moment(db, MOMENTS[1], scripted_pair(), snapshot_for_second=drop_an_item)
    results = _by_id(metrics([run]))

    assert run.snapshot_changed_on_reload and not results["GC9-snapshot-reload-change-count"].passed
    assert any("PM-009" in d for d in run.divergences)  # and the missing item shows in the brief itself


def test_an_item_that_changes_status_between_generations_is_caught(tmp_path, monkeypatch):
    real, calls = pm23_cases.generate_morning_brief, {"n": 0}

    def moves_an_item(facts, gateway, **kw):
        brief = real(facts, gateway, **kw)
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            content = re.sub(r"(## Aisha Rahman\n- Committed: [^\n]*\n- Delivered: [^\n]*\n)- Pending:( [^\n]*)\n- Blocked: none\.",
                             r"\1- Pending: none.\n- Blocked:\2", brief.content)
            brief = brief.model_copy(update={"content": content})
        return brief

    monkeypatch.setattr(pm23_cases, "generate_morning_brief", moves_an_item)

    runs = _runs(tmp_path)

    assert any("PM-017" in d and "Pending" in d for run in runs for d in run.divergences)
    assert _by_id(metrics(runs))["GC9-fact-divergence-count"].measured > 0


def test_both_generations_wrong_in_the_same_way_is_caught_by_the_snapshot_check(tmp_path, monkeypatch):
    """Two briefs that agree because the generator loses the same line both times: zero divergence, but not the snapshot's facts."""
    real = pm23_cases.generate_morning_brief

    def always_loses_a_line(facts, gateway, **kw):
        brief = real(facts, gateway, **kw)
        content = re.sub(r"- Pending: [^\n]*PM-017[^\n]*", "- Pending: none.", brief.content)  # whatever the wording
        return brief.model_copy(update={"content": content})

    monkeypatch.setattr(pm23_cases, "generate_morning_brief", always_loses_a_line)

    runs = _runs(tmp_path)
    results = _by_id(metrics(runs))

    assert results["GC9-fact-divergence-count"].measured == 0  # they agree with each other...
    assert results["GC9-snapshot-fact-gap-count"].measured > 0 and not results["GC9-snapshot-fact-gap-count"].passed  # ...and are both wrong


def test_two_identical_briefs_cannot_pass_as_a_determinism_test(tmp_path):
    runs = _runs(tmp_path, gateway_pair=lambda: (WordedGateway(""), WordedGateway("")))

    results = _by_id(metrics(runs))

    assert results["GC9-fact-divergence-count"].measured == 0
    assert results["GC9-wording-difference-count"].measured == 0 and not results["GC9-wording-difference-count"].passed


def test_a_blocker_ranking_that_is_not_stable_is_caught(tmp_path, monkeypatch):
    real, calls = pm23_cases.compute_morning_brief_facts, {"n": 0}

    def reversed_second_time(snapshot):
        facts = real(snapshot)
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            facts.blockers = list(reversed(facts.blockers))
        return facts

    monkeypatch.setattr(pm23_cases, "compute_morning_brief_facts", reversed_second_time)

    runs = _runs(tmp_path)

    assert any("blocker" in d for run in runs for d in run.divergences)


def test_the_facts_step_and_the_grounded_generation_are_deterministic_for_real(tmp_path):
    """No perturbation: computing the facts twice from one snapshot gives equal facts, and so does a repeat generation."""
    from pm.reporting.facts import compute_morning_brief_facts
    from pm.state.snapshot import build_current_snapshot

    db = build_pristine_database(tmp_path)
    snapshot = build_current_snapshot(db, taken_at=MOMENTS[1], tz_name="Asia/Colombo")

    first, second = compute_morning_brief_facts(snapshot), compute_morning_brief_facts(snapshot)

    assert first.model_dump() == second.model_dump()
    assert isinstance(pm23_cases.generate_morning_brief(first, WordedGateway("")), MorningBrief)
    assert facts_from_structure(first) == facts_from_structure(second)


# --- the script ------------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def script():
    spec = importlib.util.spec_from_file_location("check_fact_determinism_script", REPO / "scripts" / "check_fact_determinism.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_prints_the_comparison_and_exits_zero(script, capsys):
    code = script.main([])

    out = capsys.readouterr().out
    assert code == 0 and "total divergences across the two generations: 0" in out and "GC9-fact-divergence-count" in out and "PASS" in out


def test_the_script_exits_one_when_the_facts_diverge(script, capsys, monkeypatch):
    real, calls = pm23_cases.compute_morning_brief_facts, {"n": 0}

    def flaky(snapshot):
        facts = real(snapshot)
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            person = _first_delivered(facts)
            person.delivered = person.delivered[1:]
        return facts

    monkeypatch.setattr(pm23_cases, "compute_morning_brief_facts", flaky)

    code = script.main([])

    assert code == 1 and "FAIL" in capsys.readouterr().out


def test_an_unassigned_item_is_a_checked_fact_a_brief_that_drops_or_misstates_it_is_caught():
    from pm.eval.pm23_cases import facts_from_structure, facts_from_text
    from pm.reporting.facts import MorningBriefFacts, UnassignedFact

    facts = MorningBriefFacts(as_of="2026-09-18T12:00:00+00:00", people=[], blockers=[],
                              unassigned=[UnassignedFact(item_id="PM-018", title="Caching layer PR review", status="in_review")])
    shown = "Sprint scope: no sprint on file covers this date.\n\n## Nobody is assigned\n- PM-018 (Caching layer PR review): nobody is assigned; status in_review.\n\n## Blockers\n- none."
    dropped = shown.replace("## Nobody is assigned\n- PM-018 (Caching layer PR review): nobody is assigned; status in_review.\n\n", "")
    misstated = shown.replace("status in_review", "status done")

    expected = facts_from_structure(facts)

    assert ("unassigned", "PM-018", "in_review") in expected
    assert facts_from_text(shown) == expected  # said exactly as the structure has it
    assert facts_from_text(dropped) != expected and facts_from_text(misstated) != expected
    assert not any(f[0] == "owner" and f[1] == "Nobody is assigned" for f in facts_from_text(shown))  # never mistaken for a person


def test_an_unreferenced_commit_is_a_checked_fact_a_brief_that_drops_it_is_caught():
    from pm.eval.pm23_cases import facts_from_structure, facts_from_text
    from pm.reporting.facts import MorningBriefFacts, UnreferencedCommit

    facts = MorningBriefFacts(as_of="2026-09-18T12:00:00+00:00", people=[], blockers=[], unreferenced_commits=[
        UnreferencedCommit(sha="b8888bb", subject="chore: bump CI runner image to node 20", author="Wei Chen", committed_on="2026-09-16")])
    shown = ("Sprint scope: no sprint on file covers this date.\n\n## Commits with no item reference\n"
             "- b8888bb: chore: bump CI runner image to node 20 (Wei Chen, 2026-09-16).\n\n## Blockers\n- none.")
    dropped = shown.replace("## Commits with no item reference\n- b8888bb: chore: bump CI runner image to node 20 (Wei Chen, 2026-09-16).\n\n", "")

    expected = facts_from_structure(facts)

    assert ("unreferenced_commit", "b8888bb") in expected and facts_from_text(shown) == expected and facts_from_text(dropped) != expected
    assert not any(f[0] == "owner" for f in facts_from_text(shown))
