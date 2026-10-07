"""PM-21: versioned prompt files for the brief, the end-of-day summary and the mitigation.

Each is a versioned file in the registry, never a string in code, and every eval run records which
version of each produced its numbers. Acceptance: the three prompts, with their versions, are in
the results file (see test_the_committed_results_carry_the_three_prompts_with_their_versions).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from spine.prompts.registry import PromptNotFoundError, PromptRegistry

from pm.prompts import (
    BRIEF,
    MITIGATION,
    PROMPT_ROLES,
    SUMMARY,
    SUMMARY_CAPABILITY,
    fingerprint,
    format_prompts,
    prompt_record,
    stale_prompts,
)
from pm.reporting.morning_brief import MORNING_BRIEF_CAPABILITY
from pm.risk.proposals import PROMOTION_CAPABILITY

REPO = Path(__file__).resolve().parents[2]
REGISTRY = PromptRegistry(REPO / "prompts")


# --- three prompts, each a versioned file in the registry -----------------------------------------------------------


def test_the_three_roles_are_the_brief_the_summary_and_the_mitigation():
    assert PROMPT_ROLES == {
        BRIEF: "pm08_morning_brief", SUMMARY: "pm22_end_of_day_summary", MITIGATION: "pm19_risk_promotion",
    }
    assert PROMPT_ROLES[BRIEF] == MORNING_BRIEF_CAPABILITY and PROMPT_ROLES[SUMMARY] == SUMMARY_CAPABILITY
    assert PROMPT_ROLES[MITIGATION] == PROMOTION_CAPABILITY  # the names the code actually loads


@pytest.mark.parametrize("role", [BRIEF, SUMMARY, MITIGATION])
def test_each_prompt_is_a_versioned_file_in_the_registry(role):
    prompt = REGISTRY.get(PROMPT_ROLES[role])

    assert prompt.version.startswith("v") and prompt.version[1:].isdigit() and prompt.text.strip()
    assert (REPO / "prompts" / PROMPT_ROLES[role] / f"{prompt.version}.md").exists()
    assert PROMPT_ROLES[role] in REGISTRY.list_capabilities()


@pytest.mark.parametrize("role", [BRIEF, SUMMARY, MITIGATION])
def test_the_registry_refuses_a_version_that_does_not_exist(role):
    with pytest.raises(PromptNotFoundError):
        REGISTRY.get(PROMPT_ROLES[role], version="v999")


def test_a_prompt_can_be_pinned_to_an_older_version():
    """The brief has four versions; asking for v1 gets v1, not the latest."""
    assert REGISTRY.get(PROMPT_ROLES[BRIEF], version="v1").version == "v1"
    assert REGISTRY.get(PROMPT_ROLES[BRIEF]).version == "v4"


def test_the_prompts_fill_in_with_the_values_their_callers_supply():
    brief = REGISTRY.get(PROMPT_ROLES[BRIEF]).render(section_label="what each person delivered", facts_block="1. reference_id: item:PM-001",
                                                     feedback_block="")
    summary = REGISTRY.get(PROMPT_ROLES[SUMMARY]).render(section_label="what moved today", facts_block="1. reference_id: item:PM-018",
                                                         feedback_block="")
    mitigation = REGISTRY.get(PROMPT_ROLES[MITIGATION]).render(reference_id="item:PM-014", evidence="PM-014 is blocked.", feedback_block="")

    assert "item:PM-001" in brief and "item:PM-018" in summary and "item:PM-014" in mitigation
    for text in (brief, summary, mitigation):
        assert "{" not in text and "}" not in text  # nothing left unfilled


@pytest.mark.parametrize("role,values", [
    (SUMMARY, {"section_label": "x", "facts_block": "x"}),  # feedback_block missing
    (MITIGATION, {"reference_id": "x", "evidence": "x"}),
])
def test_a_missing_value_is_an_error_not_a_blank(role, values):
    with pytest.raises(ValueError):
        REGISTRY.get(PROMPT_ROLES[role]).render(**values)


@pytest.mark.parametrize("role", [BRIEF, SUMMARY, MITIGATION])
def test_every_prompt_holds_the_model_to_the_evidence(role):
    """The rules that make a line checkable: its own reference, a verbatim quote, no invented facts."""
    text = REGISTRY.get(PROMPT_ROLES[role]).text

    assert "reference_id" in text and "quote" in text and "at least 8 characters" in text
    assert "character for character" in text or "character-for-character" in text
    assert "discarded" in text  # it says what happens to a line that breaks a rule


def test_the_mitigation_prompt_asks_for_the_mitigation_line_and_names_its_verbs():
    text = REGISTRY.get(PROMPT_ROLES[MITIGATION]).text

    assert '"mitigation"' in text and "risk log entry" in text.lower()
    for verb in ("confirm", "agree", "assign", "resolve", "escalate"):
        assert verb in text


def test_the_summary_prompt_is_about_what_changed_in_the_day():
    text = REGISTRY.get(PROMPT_ROLES[SUMMARY]).text

    assert "end-of-day summary" in text and "changed" in text and "morning" in text


def test_no_prompt_text_is_a_string_in_the_code():
    """The prompts module names capabilities; it never contains a prompt."""
    source = (REPO / "src" / "pm" / "prompts.py").read_text()

    assert "You are writing" not in source and "You are drafting" not in source and "Rules, none of which" not in source


# --- what an eval run records ------------------------------------------------------------------------------------------


def test_the_record_names_the_three_prompts_with_their_versions():
    record = prompt_record(REGISTRY)

    assert set(record["prompt_roles"]) == {BRIEF, SUMMARY, MITIGATION}
    for role, capability in PROMPT_ROLES.items():
        info = record["prompt_roles"][role]
        assert info["capability"] == capability and info["version"] == REGISTRY.get(capability).version
        assert info["sha256"] == fingerprint(REGISTRY.get(capability).text) and len(info["sha256"]) == 16


def test_the_record_fingerprints_every_prompt_in_the_registry():
    assert set(prompt_record(REGISTRY)["prompt_hashes"]) == set(REGISTRY.list_capabilities())


def test_a_new_version_is_recorded_as_the_new_version(tmp_path):
    copy = tmp_path / "prompts"
    shutil.copytree(REPO / "prompts", copy)
    before = prompt_record(PromptRegistry(copy))["prompt_roles"][SUMMARY]
    (copy / SUMMARY_CAPABILITY / "v2.md").write_text("Summarise {section_label}.\n{facts_block}\n{feedback_block}\n")

    after = prompt_record(PromptRegistry(copy))["prompt_roles"][SUMMARY]

    assert (before["version"], after["version"]) == ("v1", "v2") and before["sha256"] != after["sha256"]


def test_an_edit_made_without_a_new_version_changes_the_fingerprint(tmp_path):
    copy = tmp_path / "prompts"
    shutil.copytree(REPO / "prompts", copy)
    before = prompt_record(PromptRegistry(copy))["prompt_roles"][BRIEF]
    path = copy / MORNING_BRIEF_CAPABILITY / "v4.md"
    path.write_text(path.read_text() + "\nAlso be brief.\n")

    after = prompt_record(PromptRegistry(copy))["prompt_roles"][BRIEF]

    assert before["version"] == after["version"] == "v4" and before["sha256"] != after["sha256"]


def test_a_run_whose_prompts_have_moved_on_is_reported_stale(tmp_path):
    copy = tmp_path / "prompts"
    shutil.copytree(REPO / "prompts", copy)
    registry = PromptRegistry(copy)
    record = prompt_record(registry)
    assert stale_prompts(record, registry) == []

    (copy / PROMOTION_CAPABILITY / "v2.md").write_text("Draft {reference_id} {evidence} {feedback_block}")  # a new version
    path = copy / MORNING_BRIEF_CAPABILITY / "v4.md"
    path.write_text(path.read_text() + "\nedited\n")  # an edit with no new version
    problems = stale_prompts(record, registry)

    assert any("mitigation prompt is v2 now" in p for p in problems)
    assert any("brief prompt v4 has been edited" in p for p in problems)


def test_a_record_missing_a_prompt_is_stale():
    record = prompt_record(REGISTRY)
    del record["prompt_roles"][SUMMARY]

    assert stale_prompts(record, REGISTRY) == ["the summary prompt is not recorded"]


def test_the_prompts_table_is_printable():
    text = format_prompts(REGISTRY)

    for capability in PROMPT_ROLES.values():
        assert capability in text
    assert "brief" in text and "summary" in text and "mitigation" in text and "v4" in text


# --- scripts/run_eval.py prints the prompts and records them ---------------------------------------------------------------


def _run_the_eval_script(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("run_eval_script_21", REPO / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    results = tmp_path / "results.jsonl"
    monkeypatch.setattr(module, "RESULTS_PATH", results)
    monkeypatch.setattr("sys.argv", ["run_eval.py"])
    return module.main(), results


def test_the_eval_script_prints_the_three_prompts_and_their_versions(monkeypatch, tmp_path, capfd):
    code, _ = _run_the_eval_script(monkeypatch, tmp_path)

    out = capfd.readouterr().out
    assert code == 0 and "Prompts (versioned files" in out
    assert "pm08_morning_brief" in out and "pm22_end_of_day_summary" in out and "pm19_risk_promotion" in out
    assert out.index("Prompts (versioned files") < out.index("Golden case 3")  # printed first


def test_the_eval_script_records_the_three_prompts_in_the_results_file(monkeypatch, tmp_path):
    _, results = _run_the_eval_script(monkeypatch, tmp_path)

    record = json.loads(results.read_text().splitlines()[-1])

    assert set(record["prompt_roles"]) == {BRIEF, SUMMARY, MITIGATION}
    assert record["prompt_roles"][SUMMARY]["version"] == "v1" and record["prompt_roles"][BRIEF]["version"] == "v4"
    assert record["prompt_versions"]["pm22_end_of_day_summary"] == "v1"  # and the all-prompts map has it too
    assert stale_prompts(record, REGISTRY) == []
