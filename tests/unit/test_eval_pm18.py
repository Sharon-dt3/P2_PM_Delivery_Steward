"""PM-18, golden case 4: gap precision and no duplicate.

The row: build precision and recall on the gap set, then the reject-and-rerun
assertion. Acceptance: both printed and committed (the numbers are printed by
scripts/run_eval.py and recorded in eval/results.jsonl).

A perfect score only means something if the probe can fail, so most of this file
breaks the detector and the rejection memory on purpose and checks that GC4 notices.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval

from pm.eval import pm18_cases
from pm.eval.pm18_cases import measure_gc4, precision_recall, score_gap_set
from pm.eval.registrations import register_all
from pm.risk import gaps, memory, proposals


def _by_id(results):
    return {r.metric_id: r for r in results}


# --- the acceptance test: both measured, printed and recorded -------------------------------------------------------------


def test_precision_recall_and_the_rerun_assertion_all_pass():
    results = _by_id(measure_gc4())

    assert results["GC4-gap-precision"].measured == 1.0 and results["GC4-gap-precision"].passed
    assert results["GC4-gap-recall"].measured == 1.0 and results["GC4-gap-recall"].passed
    assert results["GC4-duplicate-count"].measured == 0 and results["GC4-duplicate-count"].passed
    assert results["GC4-missed-reproposal-count"].measured == 0 and results["GC4-missed-reproposal-count"].passed


def test_the_details_say_what_was_covered():
    results = _by_id(measure_gc4())

    assert "true positives" in results["GC4-gap-precision"].detail and "scenarios" in results["GC4-gap-precision"].detail
    assert "rejected" in results["GC4-duplicate-count"].detail and "rerun x3" in results["GC4-duplicate-count"].detail
    assert "change stated" in results["GC4-missed-reproposal-count"].detail


def test_it_is_registered_and_the_numbers_are_recorded_in_the_results_file(tmp_path):
    registry = GoldenCaseRegistry()
    register_all(registry)
    assert "GC4" in {case.case_id for case in registry.all_cases()}
    results_path = tmp_path / "results.jsonl"

    summary = run_eval(registry, model_id="scripted", results_path=results_path)

    ids = {r.metric_id for r in summary.results}
    assert {"GC4-gap-precision", "GC4-gap-recall", "GC4-duplicate-count", "GC4-missed-reproposal-count"} <= ids and summary.all_passed
    recorded = {r["metric_id"] for line in results_path.read_text().splitlines() for r in json.loads(line)["results"]}
    assert "GC4-gap-precision" in recorded and "GC4-duplicate-count" in recorded


def test_the_eval_script_prints_the_precision_recall_report(monkeypatch, tmp_path, capfd):
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval_script_18", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RESULTS_PATH", tmp_path / "results.jsonl")
    monkeypatch.setattr("sys.argv", ["run_eval.py"])

    code = module.main()

    out = capfd.readouterr().out
    assert code == 0
    assert "Golden case 4" in out and "precision 1.00" in out and "recall 1.00" in out and "PM-014" in out
    assert "GC4-gap-precision" in out and "GC4-duplicate-count" in out


def test_the_hand_labels_are_the_seeds_not_the_detectors():
    """The labels are literals read off the seed; they must not be computed by the code being graded."""
    labels = {name: scenario.expected for _, scenario in pm18_cases.SEEDED for name in [scenario.name]}

    assert labels == {"seeded, 18 Sep": {"PM-014", "PM-015"}, "seeded, 16 Sep": {"PM-014"}, "seeded, 12 Sep": set()}
    source = Path(pm18_cases.__file__).read_text()
    assert "expected=" not in source.split("def score_gap_set")[1].split("def _ratio")[0]  # the scorer never derives an expectation


def test_the_labelled_scenarios_include_decoys_a_naive_detector_gets_wrong(tmp_path):
    names = {s.scenario.name for s in score_gap_set(tmp_path)}

    assert {"decoys", "no blockers", "seeded, 18 Sep"} <= names


# --- the numbers can fail: break the detector -----------------------------------------------------------------------------


def _measure(tmp_path):
    return precision_recall(score_gap_set(tmp_path))


def test_a_detector_that_only_counts_open_risks_as_cover_loses_precision(tmp_path, monkeypatch):
    monkeypatch.setattr(gaps, "covered_items", lambda snapshot: {r.related_item_id for r in snapshot.risks if r.related_item_id and r.status == "open"})

    precision, recall = _measure(tmp_path)

    assert precision < 1.0 and recall == 1.0  # PM-102 and PM-103 reported though a risk covers them


def test_a_detector_that_ignores_the_risk_log_loses_precision(tmp_path, monkeypatch):
    monkeypatch.setattr(gaps, "covered_items", lambda snapshot: set())

    precision, _ = _measure(tmp_path)

    assert precision < 1.0


def test_a_detector_that_treats_every_item_as_a_blocker_loses_precision(tmp_path, monkeypatch):
    real = gaps.find_gaps

    def eager(snapshot):
        relabelled = snapshot.model_copy(update={"items": [i.model_copy(update={"status": "blocked"}) for i in snapshot.items]})
        return real(relabelled)

    monkeypatch.setattr(gaps, "find_gaps", eager)

    precision, _ = _measure(tmp_path)

    assert precision < 1.0


def test_a_detector_that_misses_the_unassigned_blocker_loses_recall(tmp_path, monkeypatch):
    real = gaps.find_gaps
    monkeypatch.setattr(gaps, "find_gaps", lambda snapshot: [g for g in real(snapshot) if g.assignee_id])

    precision, recall = _measure(tmp_path)

    assert recall < 1.0 and precision == 1.0


def test_a_detector_that_drops_a_gap_loses_recall(tmp_path, monkeypatch):
    real = gaps.find_gaps
    monkeypatch.setattr(gaps, "find_gaps", lambda snapshot: real(snapshot)[:-1])

    _, recall = _measure(tmp_path)

    assert recall < 1.0


def test_gc4_fails_when_the_detector_is_wrong(monkeypatch):
    monkeypatch.setattr(gaps, "covered_items", lambda snapshot: set())

    results = _by_id(measure_gc4())

    assert not results["GC4-gap-precision"].passed and "false positives" in results["GC4-gap-precision"].detail


# --- the numbers can fail: break the rejection memory ------------------------------------------------------------------------


def test_a_proposer_with_no_memory_produces_duplicates(monkeypatch):
    monkeypatch.setattr(memory, "recall", lambda gap, earlier: memory.Memory(memory.NEW))
    monkeypatch.setattr(memory, "fingerprint", lambda gap: uuid.uuid4().hex)  # and nothing stops a second row either

    results = _by_id(measure_gc4())

    assert results["GC4-duplicate-count"].measured > 0 and not results["GC4-duplicate-count"].passed


def test_a_proposer_that_never_proposes_again_is_caught_by_the_flip_side(monkeypatch):
    real = memory.recall

    def forever_suppressed(gap, earlier):
        remembered = real(gap, earlier)
        if remembered.state == memory.CHANGED_SINCE_REJECTION:
            return memory.Memory(memory.REJECTED_UNCHANGED, remembered.proposal)
        return remembered

    monkeypatch.setattr(memory, "recall", forever_suppressed)

    results = _by_id(measure_gc4())

    assert results["GC4-duplicate-count"].measured == 0  # no duplicates...
    assert results["GC4-missed-reproposal-count"].measured > 0 and not results["GC4-missed-reproposal-count"].passed  # ...but a real change is lost


def test_a_proposer_that_does_not_state_the_change_is_caught(monkeypatch):
    monkeypatch.setattr(memory, "_differences", lambda previous, now, names: [])

    results = _by_id(measure_gc4())

    assert results["GC4-missed-reproposal-count"].measured > 0


def test_a_proposer_that_creates_nothing_cannot_pass_by_doing_nothing(monkeypatch):
    """With no proposals at all the probe has nothing to reject: that is a failure, not a clean zero."""
    monkeypatch.setattr(proposals, "find_gaps", lambda snapshot: [])

    results = _by_id(measure_gc4())

    assert results["GC4-duplicate-count"].measured > 0 and "setup failed" in results["GC4-duplicate-count"].detail


@pytest.mark.parametrize("field", ["blocked_since"])
def test_the_re_proposal_must_name_the_field_that_changed(monkeypatch, field):
    real = memory._differences
    monkeypatch.setattr(memory, "_differences", lambda previous, now, names: [{**d, "field": "other"} for d in real(previous, now, names)])

    results = _by_id(measure_gc4())

    assert results["GC4-missed-reproposal-count"].measured > 0
