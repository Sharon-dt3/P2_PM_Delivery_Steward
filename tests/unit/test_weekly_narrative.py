"""PM-29's narrative: Python computes every quantity, a model writes only the plain-language prose, and that prose is held to the facts.

Two parts are model-written. The lines (one per fact, plainer words) are grounded like the morning brief's: each cites its fact and quotes it, and a
line that does not is dropped, logged and shown as the recorded fact. The closing sentence follows P1's weekly roll-up rule: it may state no figure, and
that is enforced in code, so a model that ignores the prompt is refused and asked again, and after three refusals the report simply has no closing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from spine.approval.proposals import ProposalStore
from spine.llm.gateway import LLMResponse
from spine.prompts.registry import PromptRegistry

from pm.jobs import weekly_report_job as job
from pm.reporting.scripted_weekly import ScriptedWeeklyGateway
from pm.reporting.weekly import compute_weekly_facts, render_weekly_report
from pm.reporting.weekly_check import check_report
from pm.reporting.weekly_narrative import (
    CLOSING_ATTEMPTS,
    CLOSING_CAPABILITY,
    NARRATIVE_CAPABILITY,
    closing_problem,
    generate_narrative,
    narrative_facts,
)
from pm.state.snapshot import build_current_snapshot

TZ = "Asia/Colombo"
FRIDAY = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
FACTS_TEXT = ("Completed this week: 3, against 2 the week before: up 50%. PM-019 was added after planning (created 2026-09-12): Fix retry. "
              "PM-023 is blocked, since 2026-09-10, 8 days: Auth flow. PM-022 has a status the tracker does not map (waiting_on_vendor): Billing.")


@pytest.fixture()
def snaps(seeded_db_path):
    return tuple(build_current_snapshot(seeded_db_path, taken_at=(FRIDAY - n * job.WEEK).isoformat(), tz_name=TZ) for n in (0, 1, 2))


@pytest.fixture()
def facts(snaps):
    return compute_weekly_facts(*snaps)


class Canned:
    """A model that returns the given texts in order, whatever it is asked."""

    def __init__(self, *replies: str) -> None:
        self.replies, self.calls, self.prompts = list(replies), 0, []

    def generate(self, prompt: str, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        return LLMResponse(text=self.replies.pop(0), provider="canned", model="canned", prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False)


# --- what the model is given ---------------------------------------------------------------------------------------------------------------


def test_the_model_is_handed_each_fact_the_report_states_and_nothing_it_would_have_to_compute(facts):
    items = {f.reference_id: f.detail for f in narrative_facts(facts)}

    assert items["figure:velocity"] == "Completed this week: 3, against 2 the week before: up 50%."
    assert items["item:PM-019"].startswith("PM-019 was added after planning (created 2026-09-12)")
    assert items["item:PM-023"].startswith("PM-023 is blocked, since 2026-09-10, 8 days")
    assert items["item:PM-022"].startswith("PM-022 has a status the tracker does not map (waiting_on_vendor)")
    assert len(items) == len({*items}) and "item:PM-014" in items  # one fact per item, each blocked item present


def test_with_nothing_to_rephrase_the_model_is_never_called():
    from pm.reporting.weekly import WeeklyFacts

    gateway = Canned()
    narrative = generate_narrative(WeeklyFacts(end_taken_at="a", start_taken_at="b", previous_taken_at="c", week_ending="2026-01-01"), gateway)

    assert gateway.calls == 0 and narrative.lines == [] and narrative.closing is None


def test_both_prompts_are_versioned_files_in_the_registry_and_render():
    registry = PromptRegistry()

    lines = registry.get(NARRATIVE_CAPABILITY).render(section_label="X", facts_block="1. reference_id: a\n   detail: b", feedback_block="")
    closing = registry.get(CLOSING_CAPABILITY).render(facts_block="1. reference_id: a\n   detail: b", feedback_block="")

    assert registry.get(NARRATIVE_CAPABILITY).version == "v1" and registry.get(CLOSING_CAPABILITY).version == "v1"
    assert 'the "X" section' in lines and "closing sentence" in closing and '{"sentence": ' in closing  # the JSON braces survive formatting


# --- the lines: grounded ---------------------------------------------------------------------------------------------------------------------


def test_each_fact_gets_a_grounded_line_citing_it_and_the_report_carries_them(facts):
    narrative = generate_narrative(facts, ScriptedWeeklyGateway())
    text = render_weekly_report(facts, narrative)

    assert [line.message_id for line in narrative.lines] == [f.reference_id for f in narrative.facts] and narrative.dropped == []
    assert "In plain language:" in text and "- PM-019 was added after planning (created 2026-09-12): Fix retry queue duplicate delivery on redelivery." in text
    assert "In short: Completed items are up against the week before, items were added after planning, and items are blocked." in text


def test_a_forged_line_is_dropped_logged_and_replaced_by_the_recorded_fact(facts):
    def forge(lines):
        forged = dict(lines[1])
        forged["text"] = forged["text"].replace("was added after planning", "was added after planning and the client praised it")
        return [lines[0], forged, *lines[2:]]

    narrative = generate_narrative(facts, ScriptedWeeklyGateway(tamper=forge, persistent=True))
    text = render_weekly_report(facts, narrative)

    assert any("praised" in d["text"] for d in narrative.dropped) and all("praised" not in line.text for line in narrative.lines)
    assert "praised" not in text
    assert "- PM-019 was added after planning (created 2026-09-12): Fix retry queue duplicate delivery on redelivery. [as recorded]" in text


def test_a_line_that_cites_a_fact_that_is_not_there_is_dropped(facts):
    def wrong_reference(lines):
        return [{**lines[0], "reference_id": "item:PM-999"}, *lines[1:]]

    narrative = generate_narrative(facts, ScriptedWeeklyGateway(tamper=wrong_reference, persistent=True))

    assert len(narrative.dropped) >= 1 and "item:PM-999" not in [line.message_id for line in narrative.lines]
    assert "figure:velocity" not in [line.message_id for line in narrative.lines]
    assert "Completed this week: 3, against 2 the week before: up 50%. [as recorded]" in render_weekly_report(facts, narrative)  # still said, flagged


def test_a_line_that_brings_in_a_number_the_fact_does_not_have_is_dropped(facts):
    def invent(lines):
        return [{**lines[0], "text": lines[0]["text"] + " That is 99% better."}, *lines[1:]]

    narrative = generate_narrative(facts, ScriptedWeeklyGateway(tamper=invent, persistent=True))

    assert all("99" not in line.text for line in narrative.lines) and any("99" in d["text"] for d in narrative.dropped)


def test_a_forged_first_attempt_is_retried_and_the_second_is_used(facts):
    narrative = generate_narrative(facts, ScriptedWeeklyGateway(tamper=lambda lines: [{**lines[0], "text": "Everything is wonderful."}, *lines[1:]]))

    assert narrative.dropped == [] and narrative.lines[0].text.startswith("Completed this week")  # the retry came back faithful


# --- the closing sentence: no figure, no cause, nothing the facts do not say ------------------------------------------------------------------------


GOOD = "More work was completed than the week before, items were added after planning, and some items remain blocked."


def test_a_faithful_closing_sentence_is_accepted():
    assert closing_problem(GOOD, FACTS_TEXT) is None


@pytest.mark.parametrize(("sentence", "reason"), [
    ("Completed items are up 50% on the week before.", "no digit"),
    ("Completed items are up against the week before, and one item is blocked.", "no quantity"),
    ("Two items were added after planning.", "no quantity"),
    ("More work was completed, and PM-023 is blocked.", "no digit"),  # an id has digits, so the digit rule catches it first
    ("More work was completed because items were added after planning.", "must not say why"),
    ("More work was completed due to the added items.", "must not say why"),
    ("More work was completed. Some items remain blocked.", "exactly one sentence"),
    ("", "empty"),
    ("More work was completed, and the client was delighted.", "words that are in none of the facts"),
    ("Less work was completed than the week before.", "do not say the completed count is down"),
    ("Items are blocked.", None),  # mentions blocked and the facts say so: fine
])
def test_a_closing_sentence_that_breaks_a_rule_is_refused_with_the_reason(sentence, reason):
    problem = closing_problem(sentence, FACTS_TEXT)

    assert (problem is None) if reason is None else (problem is not None and reason in problem)


def test_a_claim_the_facts_do_not_make_is_refused():
    no_blocked = FACTS_TEXT.replace("is blocked", "is stuck").replace("PM-023", "PM-024")
    no_added = FACTS_TEXT.replace("added after planning", "added later")
    down = FACTS_TEXT.replace(" up 50%", " down 50%")

    assert "no fact says an item is blocked" in closing_problem("Some items remain blocked.", no_blocked)
    assert "no fact says an item was added" in closing_problem("Items were added.", no_added)
    assert "facts do not say the completed count is up" in closing_problem("More work was completed than the week before.", down)
    assert closing_problem("Less work was completed than the week before.", down) is None


def test_a_refused_closing_is_asked_again_with_the_reason_and_the_next_one_is_used(facts):
    gateway = Canned(json.dumps({"lines": []}), json.dumps({"sentence": "Two items were blocked."}), json.dumps({"sentence": GOOD}))

    narrative = generate_narrative(facts, gateway)

    assert narrative.closing == GOOD and len(narrative.closing_refusals) == 1 and "quantity" in narrative.closing_refusals[0]
    assert "Your last sentence was refused: the sentence must state no quantity" in gateway.prompts[-1]  # the model is told why


def test_a_model_that_never_complies_leaves_the_report_without_a_closing_sentence(facts):
    bad = json.dumps({"sentence": "Completed items are up 50%."})
    gateway = Canned(json.dumps({"lines": []}), *([bad] * CLOSING_ATTEMPTS))

    narrative = generate_narrative(facts, gateway)
    text = render_weekly_report(facts, narrative)

    assert narrative.closing is None and len(narrative.closing_refusals) == CLOSING_ATTEMPTS and "In short:" not in text


# --- PM-30 still holds with a narrative; the job records what the model wrote ---------------------------------------------------------------------


def test_every_number_in_the_report_including_the_narrative_still_recomputes(facts, snaps):
    text = render_weekly_report(facts, generate_narrative(facts, ScriptedWeeklyGateway()))

    assert check_report(facts, text, *snaps) == []


def test_a_number_that_got_into_the_narrative_makes_the_report_fail_the_check(facts, snaps):
    text = render_weekly_report(facts, generate_narrative(facts, ScriptedWeeklyGateway())) + "\nIn short: completed items are up 99%."

    assert any("the text states 99" in p for p in check_report(facts, text, *snaps))


def test_the_job_offers_the_narrative_and_keeps_what_the_model_wrote_and_what_grounding_dropped(seeded_db_path):
    made = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, gateway=ScriptedWeeklyGateway())

    proposal = ProposalStore(seeded_db_path).get(made.proposal_id)
    kept = proposal.original_model_output["narrative"]
    assert "In plain language:" in proposal.payload["content"] and "In short:" in proposal.payload["content"]
    assert kept["closing"]
    assert kept["lines"][0]["reference_id"] == "figure:velocity" and kept["dropped"] == []


def test_without_a_model_the_report_is_the_quantities_alone_and_says_nothing_it_was_not_given(seeded_db_path):
    made = job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, gateway=None)

    assert "In plain language" not in made.text and "In short" not in made.text and made.problems == []
    assert ProposalStore(seeded_db_path).get(made.proposal_id).original_model_output["narrative"] is None
