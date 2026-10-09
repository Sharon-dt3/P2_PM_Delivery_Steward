"""PM-33, the full harness run: all nine golden cases are in the harness, the results are committed with the model id and prompt versions, and the README's headline
numbers are rendered from that file so they cannot be typed wrong or left behind.

Done when: results are committed with the model id and the prompt versions.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from spine.eval.cases import GoldenCaseRegistry
from spine.prompts.registry import PromptRegistry

from pm.eval import pm07_cases
from pm.eval.headline import (
    BEGIN,
    CASES,
    END,
    SCRIPTED_MODEL,
    block_of,
    latest_live,
    latest_scripted,
    load_records,
    readme_matches,
    render,
    update_readme,
)
from pm.eval.registrations import register_all
from pm.state.diff import FLAPPED, STATUS_CHANGED, ItemDelta

REPO = Path(__file__).resolve().parents[2]


def script(name):
    spec = importlib.util.spec_from_file_location(f"{name}_script", REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- all nine cases are in the harness ---------------------------------------------------------------------------------------------------


def test_all_nine_golden_cases_are_registered_with_the_harness():
    registry = GoldenCaseRegistry()
    register_all(registry)

    assert {case.case_id for case in registry.all_cases()} == set(CASES)


def test_golden_case_3_is_recorded_with_precision_recall_and_a_duplicate_count_and_passes():
    results = {m.metric_id: m for m in pm07_cases.measure_gc3()}

    assert set(results) == {"GC3-delta-precision", "GC3-delta-recall", "GC3-duplicate-entry-count"}
    assert results["GC3-delta-precision"].measured == 1.0 and results["GC3-delta-recall"].measured == 1.0 and results["GC3-duplicate-entry-count"].measured == 0
    assert all(m.passed for m in results.values())
    assert "3 of 3 labelled changes found" in results["GC3-delta-recall"].detail


def entry(item_id, kind):
    return ItemDelta(item_id=item_id, kind=kind, before_status="in_progress", after_status="in_progress", description="x")


GOOD = [entry("PM-016", FLAPPED), entry("PM-018", STATUS_CHANGED), entry("PM-020", STATUS_CHANGED)]


@pytest.mark.parametrize(("delta_items", "failing", "why"), [
    (GOOD[1:], "GC3-delta-recall", "missed: ['PM-016']"),  # the twice-moved item missed altogether
    ([*GOOD, entry("PM-099", STATUS_CHANGED)], "GC3-delta-precision", "extra: ['PM-099']"),  # something reported that did not change
    ([entry("PM-016", STATUS_CHANGED), *GOOD[1:]], "GC3-delta-recall", "missed: ['PM-016']"),  # the flap called a plain status change: a miss, not a match
    ([*GOOD, entry("PM-016", FLAPPED)], "GC3-duplicate-entry-count", "reported more than once: ['PM-016']"),  # the twice-moved item reported twice
])
def test_golden_case_3_fails_when_the_engine_misses_invents_miscalls_or_duplicates(monkeypatch, delta_items, failing, why):
    from pm.state.diff import SnapshotDelta

    monkeypatch.setattr(pm07_cases, "build_case_3_delta", lambda: SnapshotDelta(before_taken_at="a", after_taken_at="b", items=delta_items))

    results = {m.metric_id: m for m in pm07_cases.measure_gc3()}

    assert not results[failing].passed and why in results[failing].detail


# --- the committed results ---------------------------------------------------------------------------------------------------------------


def committed():
    return load_records(REPO / "eval" / "results.jsonl")


def test_the_committed_results_hold_a_full_run_with_every_case_the_model_id_and_every_prompt_version():
    record = latest_scripted(committed())

    assert record is not None, "no committed record has all nine golden cases on the scripted gateway"
    assert record["model_id"] == SCRIPTED_MODEL and record["all_passed"] is True and record["dirty"] is False
    assert {r["metric_id"].split("-", 1)[0] for r in record["results"]} == set(CASES)
    assert all(r["passed"] for r in record["results"]) and record["revision"]


def test_the_recorded_prompt_versions_are_the_prompts_the_repository_has_now():
    record = latest_scripted(committed())
    registry = PromptRegistry(REPO / "prompts")

    assert record["prompt_versions"] == {name: registry.get(name).version for name in registry.list_capabilities()}  # a prompt changed without a re-record would show here
    assert set(record["prompt_hashes"]) == set(record["prompt_versions"])


def test_the_readme_headline_numbers_are_exactly_what_the_results_file_renders():
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert block_of(readme) is not None and readme_matches(readme, committed())


# --- the renderer ----------------------------------------------------------------------------------------------------------------------------


def metric(case, name, measured, target=0, comparator="at_most", passed=True):
    return {"metric_id": f"{case}-{name}", "name": name, "measured": measured, "target": target, "comparator": comparator, "passed": passed, "detail": ""}


def full_record(model="scripted-gateway", run_at="2026-10-09T10:00:00+00:00", revision="abc1234", gc1=0.9487, **kw):
    results = [metric("GC1", "citation-rate", gc1, 0.9, "at_least"), metric("GC2", "fabricated-claim-count", 0),
               metric("GC3", "delta-precision", 1.0, 1.0, "at_least"), metric("GC3", "delta-recall", 1.0, 1.0, "at_least"),
               metric("GC4", "gap-precision", 1.0, 1.0, "at_least"), metric("GC4", "gap-recall", 1.0, 1.0, "at_least"), metric("GC4", "duplicate-count", 0),
               metric("GC5", "two-day-set-errors", 0), metric("GC5", "four-day-set-errors", 0), metric("GC5", "shrink-errors", 0),
               metric("GC6", "write-bypass-count", 0), metric("GC6", "risk-write-bypass-count", 0), metric("GC6", "tracker-write-bypass-count", 0),
               metric("GC6", "report-write-bypass-count", 0), metric("GC7", "cap-breach-count", 0), metric("GC7", "escalation-order-violation-count", 0),
               metric("GC8", "leaked-proposal-count", 0), metric("GC8", "refused-record-count", 11, 11, "at_least"), metric("GC9", "fact-divergence-count", 0)]
    return {"run_at": run_at, "model_id": model, "revision": revision, "dirty": False, "all_passed": True, "results": results,
            "prompt_versions": {"pm08_morning_brief": "v4", "pm22_end_of_day_summary": "v1"}, **kw}


def test_the_table_has_one_row_per_golden_case_with_its_headline_and_target():
    text = render([full_record()])

    rows = [line for line in text.splitlines() if line.startswith("| **GC")]
    assert [r.split("**")[1] for r in rows] == list(CASES)
    assert "citation rate 0.9487 | ≥ 0.9 | 1 of 1" in text and "fabricated claims 0 | ≤ 0" in text
    assert "precision 1, recall 1 | ≥ 1 / ≥ 1" in text and "records refused 11 | ≤ 0 / ≥ 11" in text
    assert "19 of 19 metrics pass" in text and "`pm08_morning_brief` v4" in text and "model id `scripted-gateway`" in text and "code revision `abc1234`" in text


def test_the_scripted_run_says_it_is_not_a_language_model():
    assert "not a language model" in render([full_record()])


def test_the_newest_full_scripted_run_is_used_and_a_partial_or_older_one_is_not():
    partial = full_record(run_at="2026-10-10T00:00:00+00:00", revision="partial1")
    partial["results"] = [r for r in partial["results"] if not r["metric_id"].startswith("GC3")]  # a record from before GC3 was recorded
    older, newer = full_record(revision="older11"), full_record(revision="newer22", run_at="2026-10-11T00:00:00+00:00")

    assert latest_scripted([older, newer, partial])["revision"] == "newer22"
    assert latest_scripted([partial]) is None


def test_a_real_model_run_gets_its_own_section_with_its_model_id_and_only_when_there_is_one():
    live = full_record(model="bedrock:global.anthropic.claude-sonnet-4-6", gc1=0.8, revision="live999")
    live["results"][0]["passed"] = False

    without, with_live = render([full_record()]), render([full_record(), live])

    assert "real model" not in without and latest_live([full_record()]) is None
    assert "#### The same harness on a real model" in with_live and "`bedrock:global.anthropic.claude-sonnet-4-6`" in with_live
    assert "citation rate 0.8 | ≥ 0.9" in with_live and "18 of 19 metrics pass in that run" in with_live


def test_with_no_full_scripted_run_it_refuses_to_render_rather_than_show_nothing():
    with pytest.raises(ValueError, match="no full run"):
        render([])


def test_writing_the_readme_is_idempotent_replaces_between_the_markers_and_notices_a_change():
    records = [full_record()]
    readme = "# Title\n\n## Getting started\n\nsteps\n"

    once = update_readme(readme, records)
    twice = update_readme(once, records)

    assert BEGIN in once and END in once and once.index("## Evaluation") < once.index("## Getting started") and once == twice
    assert readme_matches(once, records)
    assert not readme_matches(once.replace("citation rate 0.9487", "citation rate 0.99"), records)  # a hand-edited number is caught
    assert not readme_matches(once, [full_record(gc1=0.91)])  # a newer run than the README says so
    assert once.count(BEGIN) == 1 and "steps" in once  # nothing else in the README was touched


def test_the_readme_script_writes_and_checks(tmp_path, monkeypatch, capsys):
    module = script("eval_readme")
    readme, results = tmp_path / "README.md", tmp_path / "results.jsonl"
    readme.write_text("# T\n\n## Getting started\n", encoding="utf-8")
    results.write_text(json.dumps(full_record()) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "README", readme)
    monkeypatch.setattr(module, "RESULTS", results)

    assert module.main(["--check"]) == 1 and "does not match" in capsys.readouterr().out
    assert module.main(["--write"]) == 0
    assert module.main(["--check"]) == 0 and "matches" in capsys.readouterr().out


# --- the eval never reaches the outside world --------------------------------------------------------------------------------------------------


def test_an_eval_run_pins_everything_that_could_post_mirror_or_rewrite_whatever_the_environment_holds(monkeypatch):
    module = script("run_eval")
    for name, value in {"TEAMS_PUBLISHER_MODE": "power_automate", "POWER_AUTOMATE_FLOW_URL": "https://example.invalid/flow", "PM_SUPABASE_MIRROR": "1",
                        "SUPABASE_DB_URL": "postgresql://x", "PM_RISK_LOG_SYNC": "1", "PM_AUTO_APPROVE": "1", "PM_CHANNEL_BRIEFS": "a,b", "PM_WEEKLY_REPORT": "1"}.items():
        monkeypatch.setenv(name, value)

    module.isolate_from_the_outside_world()

    assert os.environ["TEAMS_PUBLISHER_MODE"] == "mock" and os.environ["POWER_AUTOMATE_FLOW_URL"] == "" and os.environ["PM_SUPABASE_MIRROR"] == "0"
    assert os.environ["SUPABASE_DB_URL"] == "" and os.environ["PM_RISK_LOG_SYNC"] == "0" and os.environ["PM_AUTO_APPROVE"] == "0"
    assert os.environ["PM_CHANNEL_BRIEFS"] == "" and os.environ["PM_WEEKLY_REPORT"] == ""


def test_only_the_models_own_settings_are_read_from_the_env_file_and_never_the_rest(tmp_path, monkeypatch):
    module = script("run_eval")
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=bedrock\nBEDROCK_MODEL_ID=arn:x/model-a\nAWS_REGION=us-east-2\nTEAMS_PUBLISHER_MODE=power_automate\n"
                   "POWER_AUTOMATE_FLOW_URL=https://example.invalid/secret\nSUPABASE_DB_URL=postgresql://u:p@h/db\nPM_AUTO_APPROVE=1\nPM_APPROVER_IDS=someone\n", encoding="utf-8")
    for name in ("LLM_PROVIDER", "BEDROCK_MODEL_ID", "AWS_REGION", "TEAMS_PUBLISHER_MODE", "POWER_AUTOMATE_FLOW_URL", "SUPABASE_DB_URL", "PM_AUTO_APPROVE", "PM_APPROVER_IDS"):
        monkeypatch.delenv(name, raising=False)

    module.load_model_settings(env)

    assert (os.environ["LLM_PROVIDER"], os.environ["BEDROCK_MODEL_ID"], os.environ["AWS_REGION"]) == ("bedrock", "arn:x/model-a", "us-east-2")
    for name in ("TEAMS_PUBLISHER_MODE", "POWER_AUTOMATE_FLOW_URL", "SUPABASE_DB_URL", "PM_AUTO_APPROVE", "PM_APPROVER_IDS"):
        assert name not in os.environ, name


def test_a_setting_already_in_the_environment_is_not_overridden_by_the_file(tmp_path, monkeypatch):
    module = script("run_eval")
    (tmp_path / ".env").write_text("BEDROCK_MODEL_ID=from-the-file\n", encoding="utf-8")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "from-the-shell")

    module.load_model_settings(tmp_path / ".env")

    assert os.environ["BEDROCK_MODEL_ID"] == "from-the-shell"


def test_the_model_id_in_the_record_is_the_models_name_never_the_account_specific_arn(monkeypatch):
    module = script("run_eval")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "arn:aws:bedrock:us-east-2:123456789012:inference-profile/global.anthropic.claude-sonnet-4-6")

    label = module.bedrock_model_label()

    assert label == "bedrock:global.anthropic.claude-sonnet-4-6" and "123456789012" not in label and "arn:" not in label


def test_with_no_model_configured_a_live_run_stops_and_says_so(monkeypatch):
    module = script("run_eval")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)

    with pytest.raises(SystemExit, match="BEDROCK_MODEL_ID is not set"):
        module.bedrock_model_label()
