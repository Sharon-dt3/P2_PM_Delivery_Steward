"""GC2's probe and the labels the brief puts in front of a blocker line.

The brief renders a blocker as `[severity] RISK-id: line`, adding the tag and the id itself when the model left them out (they come from the risk log).
A real model (Claude) leaves them out; the scripted gateway writes them. The probe used to compare the rendered line with the model's text exactly, so every
line from a model that left the labels out was counted as a fabrication: two of them in the first live run on Claude. The probe now works the expected label
out from the facts, and a label that is NOT the risk's own is still a fabrication.
"""

from __future__ import annotations

from spine.grounding.kernel import FactualLine

from pm.eval.pm12_cases import ScriptedGateway, build_seeded_facts, count_fabrications
from pm.reporting.morning_brief import generate_morning_brief


def scripted():
    facts = build_seeded_facts()
    return facts, generate_morning_brief(facts, ScriptedGateway())


def without_labels(facts, brief):
    """The brief as a model that writes just the sentence produces it: the grounded line is bare, and the rendering has put the labels in front."""
    bare = []
    for line in brief.sections["blockers"]:
        blocker = next(b for b in facts.blockers if line.message_id.endswith(b.risk_id))
        text = line.text.replace(f"[{blocker.severity}] ", "").replace(f"{blocker.risk_id}: ", "")
        bare.append(FactualLine(text=text, message_id=line.message_id, quote=line.quote))
    content = brief.content
    for old, new in zip(brief.sections["blockers"], bare, strict=True):
        blocker = next(b for b in facts.blockers if old.message_id.endswith(b.risk_id))
        content = content.replace(f"- {old.text}", f"- [{blocker.severity}] {blocker.risk_id}: {new.text}")
    return brief.model_copy(update={"sections": {**brief.sections, "blockers": bare}, "content": content})


def test_the_scripted_brief_is_still_clean():
    facts, brief = scripted()

    assert count_fabrications(brief, facts) == []


def test_a_model_that_writes_only_the_sentence_is_not_a_fabrication_when_the_brief_adds_the_labels():
    facts, brief = scripted()
    bare = without_labels(facts, brief)

    assert all(not line.text.startswith("[") for line in bare.sections["blockers"])  # the model's own text has no label
    assert any("] RISK-" in line for line in bare.content.splitlines() if line.startswith("- "))  # and the brief shows one
    assert count_fabrications(bare, facts) == []


def test_a_wrong_severity_in_front_of_a_blocker_is_still_a_fabrication():
    facts, brief = scripted()
    bare = without_labels(facts, brief)
    blocker = facts.blockers[0]
    wrong = "low" if blocker.severity != "low" else "high"

    forged = bare.model_copy(update={"content": bare.content.replace(f"[{blocker.severity}] {blocker.risk_id}:", f"[{wrong}] {blocker.risk_id}:", 1)})

    problems = count_fabrications(forged, facts)
    assert any("is labelled" in p and f"[{wrong}] {blocker.risk_id}:" in p for p in problems), problems


def test_another_risks_id_in_front_of_a_blocker_is_still_a_fabrication():
    facts, brief = scripted()
    bare = without_labels(facts, brief)
    first, second = facts.blockers[0], facts.blockers[1]

    forged = bare.model_copy(update={"content": bare.content.replace(f"{first.risk_id}:", f"{second.risk_id}:", 1)})

    assert any("is labelled" in p for p in count_fabrications(forged, facts))


def test_a_line_that_is_not_a_blocker_line_at_all_is_still_refused():
    facts, brief = scripted()
    invented = brief.model_copy(update={"content": brief.content.replace("## Blockers\n", "## Blockers\n- [high] RISK-099: The vendor has cancelled the contract.\n", 1)})

    assert any("neither a grounded blocker line nor a marked fact" in p for p in count_fabrications(invented, facts))
