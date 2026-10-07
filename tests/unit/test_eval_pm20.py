"""PM-20, golden case 5: promotion threshold reconfiguration.

The row: run at a two-day threshold, then at four days, and assert the proposed set shrinks
correctly. Acceptance: both runs printed and the set differences asserted. The point is to prove
the threshold is really configuration: the same code, the same project, only the number in the
configuration file changes.

A perfect score only means something if it can fail, so most of this file breaks the code on
purpose (a literal threshold, a threshold read once and remembered, an off-by-one, a proposer that
ignores the policy) and checks GC5 notices.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval

from pm.eval.pm20_cases import (
    AGES,
    EXPECTED_ADDED,
    EXPECTED_AT_FOUR,
    EXPECTED_AT_TWO,
    EXPECTED_DROPPED,
    HAND_BUILT_BLOCKERS,
    HIGH,
    LOW,
    format_report,
    grade,
    measure_gc5,
    run_both_thresholds,
)
from pm.eval.registrations import register_all
from pm.risk import promotion_config, proposals
from pm.risk.promotion_config import PromotionPolicy


def _by_id(results):
    return {r.metric_id: r for r in results}


# --- the acceptance test: both runs printed, set differences asserted ----------------------------------------------------


def test_both_runs_are_printed_and_the_set_differences_are_asserted(tmp_path, capsys):
    results = run_both_thresholds(tmp_path)
    print(format_report(results))  # the same text scripts/run_eval.py prints

    for name, runs in results.items():
        assert runs[LOW] == EXPECTED_AT_TWO == {"PM-104", "PM-014", "PM-105", "PM-106", "PM-107"}, name
        assert runs[HIGH] == EXPECTED_AT_FOUR == {"PM-106", "PM-107"}, name
        assert runs[LOW] - runs[HIGH] == EXPECTED_DROPPED == {"PM-104", "PM-014", "PM-105"}, name  # what dropped out
        assert runs[HIGH] - runs[LOW] == EXPECTED_ADDED == set(), name  # and nothing new appeared
        assert runs[HIGH] < runs[LOW], name  # the four-day set is a strict subset of the two-day set: it shrank
    printed = capsys.readouterr().out
    assert "threshold 2 days" in printed and "threshold 4 days" in printed and "dropped [PM-104, PM-014, PM-105]" in printed
    assert "newly proposed [] (expected none)" in printed


def test_all_four_numbers_are_zero():
    results = _by_id(measure_gc5())

    for metric in ("GC5-two-day-set-errors", "GC5-four-day-set-errors", "GC5-shrink-errors", "GC5-entry-point-disagreement-count"):
        assert results[metric].measured == 0 and results[metric].passed, (metric, results[metric].detail)


def test_the_details_show_the_sets():
    results = _by_id(measure_gc5())

    assert "PM-104" in results["GC5-two-day-set-errors"].detail and "PM-106" in results["GC5-four-day-set-errors"].detail
    assert "dropped [PM-104, PM-014, PM-105]" in results["GC5-shrink-errors"].detail


def test_the_report_shows_both_entry_points_and_the_ages(tmp_path):
    text = format_report(run_both_thresholds(tmp_path))

    assert "via the morning job" in text and "via detect_risks.py" in text
    assert "PM-014 4" in text and "PM-015 1" in text and "PM-107 10" in text  # the hand-labelled ages
    assert text.count("[ok ]") == 6 and "BAD" not in text


def test_it_is_registered_and_recorded_in_the_results_file(tmp_path):
    registry = GoldenCaseRegistry()
    register_all(registry)
    assert "GC5" in {case.case_id for case in registry.all_cases()}
    results_path = tmp_path / "results.jsonl"

    summary = run_eval(registry, model_id="scripted", results_path=results_path)

    ids = {r.metric_id for r in summary.results}
    assert {"GC5-two-day-set-errors", "GC5-four-day-set-errors", "GC5-shrink-errors", "GC5-entry-point-disagreement-count"} <= ids
    assert summary.all_passed
    recorded = {r["metric_id"] for line in results_path.read_text().splitlines() for r in json.loads(line)["results"]}
    assert "GC5-shrink-errors" in recorded


def test_the_eval_script_prints_both_runs(monkeypatch, tmp_path, capfd):
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval_script_20", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RESULTS_PATH", tmp_path / "results.jsonl")
    monkeypatch.setattr("sys.argv", ["run_eval.py"])

    code = module.main()

    out = capfd.readouterr().out
    assert code == 0 and "Golden case 5" in out and "threshold 2 days" in out and "threshold 4 days" in out
    assert "GC5-shrink-errors" in out and "going from 2 to 4 days" in out


# --- the labels are the hand's, and consistent -----------------------------------------------------------------------------


def test_the_labels_follow_from_the_ages_by_plain_arithmetic():
    """The expected sets are literals, but they must agree with the ages by hand: older than the threshold."""
    assert {i for i, age in AGES.items() if age > LOW} == EXPECTED_AT_TWO
    assert {i for i, age in AGES.items() if age > HIGH} == EXPECTED_AT_FOUR
    assert EXPECTED_AT_TWO - EXPECTED_AT_FOUR == EXPECTED_DROPPED and not EXPECTED_AT_FOUR - EXPECTED_AT_TWO


def test_the_hand_built_blockers_really_have_the_labelled_ages():
    for item_id, entered_on in HAND_BUILT_BLOCKERS:
        assert (date(2026, 9, 18) - date.fromisoformat(entered_on)).days == AGES[item_id], item_id


def test_the_two_and_four_day_thresholds_are_the_ones_the_row_names():
    assert (LOW, HIGH) == (2, 4)


def test_a_blocker_exactly_at_each_threshold_is_in_the_set_that_tests_the_boundary():
    """PM-103 is exactly 2 days old and PM-014/PM-105 exactly 4: the boundary cases are in the project."""
    assert AGES["PM-103"] == LOW and AGES["PM-014"] == AGES["PM-105"] == HIGH
    assert "PM-103" not in EXPECTED_AT_TWO and "PM-014" not in EXPECTED_AT_FOUR  # "older than" is strict


def test_blockers_already_in_the_risk_log_are_never_in_either_set(tmp_path):
    results = run_both_thresholds(tmp_path)

    for runs in results.values():
        assert not ({"PM-023", "PM-024"} & (runs[LOW] | runs[HIGH]))


# --- the numbers can fail: break the code --------------------------------------------------------------------------------------


def _measure():
    return grade(run_both_thresholds_fresh())


def run_both_thresholds_fresh():
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pm_gc5_test_") as tmp:
        return run_both_thresholds(Path(tmp))


def test_a_literal_threshold_in_the_code_is_caught(monkeypatch):
    """If the number were baked in, editing the file would change nothing: the four-day run would still be a two-day run."""
    literal = lambda *a, **k: PromotionPolicy(LOW, "a literal")
    monkeypatch.setattr(promotion_config, "load_promotion_policy", literal)
    monkeypatch.setattr("pm.jobs.morning_brief_job.load_promotion_policy", literal)

    errors = _measure()

    assert errors["four"] > 0 and errors["shrink"] > 0 and not (errors["two"])  # right at two days, never shrinks


def test_a_threshold_read_once_and_remembered_is_caught(monkeypatch):
    """Reconfiguration must take effect on the next run, not after a restart."""
    real, remembered = promotion_config.load_promotion_policy, {}

    def cached(*args, **kwargs):
        remembered.setdefault("policy", real(*args, **kwargs))
        return remembered["policy"]

    monkeypatch.setattr(promotion_config, "load_promotion_policy", cached)
    monkeypatch.setattr("pm.jobs.morning_brief_job.load_promotion_policy", cached)

    errors = _measure()

    assert errors["four"] > 0 and errors["shrink"] > 0


def test_an_off_by_one_is_caught(monkeypatch):
    real = proposals.plan_promotion
    monkeypatch.setattr(proposals, "plan_promotion",
                        lambda snapshot, policy, **kw: real(snapshot, PromotionPolicy(policy.threshold_days - 1, policy.source), **kw))

    errors = _measure()

    assert errors["two"] > 0 and errors["four"] > 0 and errors["shrink"] > 0  # "at least" instead of "older than"


def test_a_proposer_that_ignores_the_policy_is_caught(monkeypatch):
    real = proposals.plan_promotion

    def everything_eligible(snapshot, policy, **kw):
        plan = real(snapshot, policy, **kw)
        plan.eligible.extend(plan.below_threshold)
        return plan

    monkeypatch.setattr(proposals, "plan_promotion", everything_eligible)

    errors = _measure()

    assert errors["two"] > 0 and errors["four"] > 0 and errors["shrink"] > 0  # both sets are wrong and nothing ever shrinks


def test_an_entry_point_that_ignores_the_configuration_is_caught(monkeypatch):
    """Only the command line is broken (the job's own loader is untouched): the two disagree."""
    monkeypatch.setattr(promotion_config, "load_promotion_policy", lambda *a, **k: PromotionPolicy(LOW, "a literal"))

    errors = _measure()

    assert errors["disagreement"] > 0


def test_gc5_fails_when_the_threshold_is_not_configuration(monkeypatch):
    literal = lambda *a, **k: PromotionPolicy(LOW, "a literal")
    monkeypatch.setattr(promotion_config, "load_promotion_policy", literal)
    monkeypatch.setattr("pm.jobs.morning_brief_job.load_promotion_policy", literal)

    results = _by_id(measure_gc5())

    assert not results["GC5-four-day-set-errors"].passed and not results["GC5-shrink-errors"].passed


def test_a_pipeline_that_proposes_nothing_cannot_pass_by_doing_nothing(monkeypatch):
    from pm.risk.promotion import PromotionPlan

    monkeypatch.setattr(proposals, "plan_promotion", lambda snapshot, policy, **kw: PromotionPlan(policy=policy, snapshot=snapshot))

    errors = _measure()

    assert errors["two"] > 0 and errors["shrink"] > 0  # five expected at two days, none proposed
