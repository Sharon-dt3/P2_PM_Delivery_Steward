"""PM-12, strengthened: GC2 is the most important number in the submission, so
its probe has to be able to fail on every kind of fabrication the brief can
produce -- not only a wrong reference. Each planted forgery below is a line a
model could really write; each is tried two ways (the model corrects itself on
retry, and the model never does), and none may survive into the brief or the
text posted to Teams. The independent check also reads every surviving line's
own words, ids, numbers and quote against its fact.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval
from spine.grounding.kernel import FactualLine

from pm.eval import pm12_cases
from pm.eval.pm12_cases import (
    FORGERIES,
    ScriptedGateway,
    build_seeded_facts,
    count_fabrications,
    measure_gc1,
    measure_gc2,
    register_gc2_probe,
)
from pm.eval.registrations import register_all
from pm.reporting.morning_brief import generate_morning_brief


@pytest.fixture()
def facts(seeded_db_path):
    return build_seeded_facts(seeded_db_path)


def _brief_with_a_surviving_line(facts, **changes):
    """A brief whose first delivered line has been replaced after the fact --
    as if grounding had let it through."""
    brief = generate_morning_brief(facts, ScriptedGateway())
    line = brief.sections["delivered"][0]
    forged = FactualLine(**{**line.model_dump(), **changes})
    sections = {**brief.sections, "delivered": [forged, *brief.sections["delivered"][1:]]}
    return brief.model_copy(update={"sections": sections, "content": brief.content.replace(line.text, forged.text, 1)})


# --- the independent check reads the words, not only the reference ------------


def test_a_surviving_line_with_an_invented_id_is_counted(facts):
    line = generate_morning_brief(facts, ScriptedGateway()).sections["delivered"][0]
    brief = _brief_with_a_surviving_line(facts, text=line.text + " and closed PM-9999")

    assert any("PM-9999" in p for p in count_fabrications(brief, facts))


def test_a_surviving_line_with_an_invented_number_is_counted(facts):
    line = generate_morning_brief(facts, ScriptedGateway()).sections["delivered"][0]
    brief = _brief_with_a_surviving_line(facts, text=line.text + " with 12 bugs fixed")

    assert any("12" in p for p in count_fabrications(brief, facts))


def test_a_surviving_line_with_an_invented_quote_is_counted(facts):
    brief = _brief_with_a_surviving_line(facts, quote="words that appear nowhere in the fact")

    assert any("quote" in p for p in count_fabrications(brief, facts))


def test_a_surviving_line_with_embellishing_words_is_counted(facts):
    line = generate_morning_brief(facts, ScriptedGateway()).sections["delivered"][0]
    brief = _brief_with_a_surviving_line(facts, text=line.text + " and got praise from the client")

    assert any("praise" in p for p in count_fabrications(brief, facts))


def test_faithful_and_paraphrased_lines_are_not_flagged(facts):
    assert count_fabrications(generate_morning_brief(facts, ScriptedGateway()), facts) == []


# --- planted forgeries, healing and persistent --------------------------------


def test_there_is_a_forgery_for_every_way_a_line_can_lie():
    assert set(FORGERIES) >= {
        "invented_id", "invented_number", "embellishment", "missing_quote", "fake_quote",
        "cross_section_reference", "nonexistent_reference", "no_reference",
    }


@pytest.mark.parametrize("persistent", [False, True], ids=["model-corrects-on-retry", "model-never-corrects"])
@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_no_planted_forgery_survives_into_the_brief(facts, name, persistent):
    forgery = FORGERIES[name]
    gateway = ScriptedGateway(**{"persistent_tamper" if persistent else "tamper": forgery.tamper(facts)})

    brief = generate_morning_brief(facts, gateway)

    assert forgery.marker not in brief.content
    assert count_fabrications(brief, facts) == []
    if persistent:  # the forged line was dropped for good; its fact is still shown, marked
        assert "[as recorded]" in brief.content


def test_the_probe_would_notice_if_a_forgery_got_through(facts):
    forgery = FORGERIES["embellishment"]
    brief = generate_morning_brief(facts, ScriptedGateway())
    # simulate a broken grounding layer: the forged text is in the brief
    leaked = brief.model_copy(update={"content": brief.content + "\n" + forgery.marker})

    assert pm12_cases._survivors(leaked, {"embellishment": forgery}, "x")


# --- GC2 as a whole ------------------------------------------------------------


def test_gc2_is_zero_and_says_which_scenarios_it_covered(facts):
    (result,) = measure_gc2(facts)

    assert result.measured == 0 and result.passed
    for scenario in ("seeded", "planted forgeries", "zero-activity", "empty day", "commit-only", "posted message", "auto-approve"):
        assert scenario in result.detail, scenario


def test_the_commit_only_scenario_is_really_in_gc2(facts):
    with_commit_only = pm12_cases._with_commit_only_person(facts)

    person = with_commit_only.people[-1]
    assert not any((person.committed, person.delivered, person.pending, person.blocked)) and person.commit_count > 0
    brief = generate_morning_brief(with_commit_only, ScriptedGateway())
    assert f"- Commits: {person.commit_count} recorded; no tracker items." in brief.content


def test_the_auto_approve_probe_is_clean_and_would_notice_an_unattended_bad_brief(monkeypatch):
    assert pm12_cases._auto_approve_problems() == []

    from pm.approval import service

    # simulate a broken safety check: auto-approve no longer looks at what grounding dropped
    monkeypatch.setattr(service, "_dropped_count", lambda proposal: 0)
    monkeypatch.setattr(service, "_AS_RECORDED", "\x00never")

    assert any("dropped lines" in p for p in pm12_cases._auto_approve_problems())


def test_a_registered_extra_probe_counts_toward_gc2(facts, monkeypatch):
    """The row says "brief and summary". The end-of-day summary is PM-22 and is
    not built yet; when it is, its probe is registered here and counts."""
    monkeypatch.setattr(pm12_cases, "GC2_EXTRA_PROBES", [])
    register_gc2_probe("end-of-day summary", lambda: ["summary: invented a blocker"])

    (result,) = measure_gc2(facts)

    assert result.measured == 1 and not result.passed and "end-of-day summary" in result.detail


def test_the_text_posted_to_teams_adds_nothing_to_the_brief(facts):
    assert pm12_cases._posted_message_problems() == []


def test_a_posted_message_with_an_extra_line_is_flagged(monkeypatch):
    from pm.approval import proposals

    real = proposals.format_brief_message
    monkeypatch.setattr(proposals, "format_brief_message", lambda *a, **k: real(*a, **k) + "\nAlso: the team loves it.")

    assert pm12_cases._posted_message_problems()


# --- GC1 on a real model, and the run record -------------------------------------


def test_a_live_gateway_is_run_once_and_feeds_both_metrics(seeded_db_path, tmp_path):
    gateways = []

    def factory():
        gateways.append(ScriptedGateway())
        return gateways[-1]

    registry = GoldenCaseRegistry()
    register_all(registry, db_path=seeded_db_path, gateway_factory=factory, live=True)
    summary = run_eval(registry, model_id="fake-live", results_path=tmp_path / "r.jsonl", extra={"revision": "abc1234"})

    assert len(gateways) == 1  # one model pass shared by GC1 and GC2
    assert summary.all_passed
    gc2 = next(r for r in summary.results if r.metric_id.startswith("GC2"))
    assert "live model brief" in gc2.detail


def test_a_live_brief_that_fabricates_would_fail_gc2(facts):
    brief = generate_morning_brief(facts, ScriptedGateway())
    leaked = brief.model_copy(update={"content": brief.content.replace("## Blockers", "## Blockers\n- RISK-777 caused an outage")})

    (result,) = measure_gc2(facts, live_brief=leaked)

    assert result.measured >= 1 and not result.passed


def test_the_run_record_carries_the_extra_fields(tmp_path):
    registry = GoldenCaseRegistry()
    path = tmp_path / "r.jsonl"
    run_eval(registry, model_id="m", results_path=path, extra={"revision": "abc1234", "dirty": False})

    record = json.loads(path.read_text().splitlines()[-1])
    assert record["revision"] == "abc1234" and record["dirty"] is False and record["model_id"] == "m"


def test_the_runner_script_can_name_the_code_revision():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("run_eval_script", Path(__file__).parents[2] / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    extra = module.code_revision()

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    assert extra["revision"] == head and isinstance(extra["dirty"], bool)


def test_gc1_on_the_scripted_default_still_counts_its_planted_slips(facts):
    (result,) = measure_gc1(facts)

    assert result.measured == 0.9487 and "37 of 39" in result.detail  # the same two planted slips; 39 lines now, because PM-022 (an unmapped status, PM-31) is no longer a model-written line
