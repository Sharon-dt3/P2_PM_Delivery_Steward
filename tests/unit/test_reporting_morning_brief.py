"""PM-08: the model-expression half.

The row's own acceptance test, literal: "Regenerating on the same
snapshot yields identical facts; wording may differ." test_reporting_facts.py
proves the "identical facts" half in isolation; this file proves the full
loop, including the model call: the SAME facts, run through a fake
gateway that deliberately returns two different narratives on two
successive calls, still come back with .facts identical both times, while
.narrative is allowed -- expected -- to differ.

No real network call happens anywhere in this file. FakeGateway mirrors
P1's own testing convention exactly (see e.g.
../P3_Agents/tests/unit/test_daily_summary.py): a duck-typed stand-in
matching LLMGateway's own `.generate(prompt, **kwargs) -> LLMResponse`
shape, not a real HTTP call -- there is no LLMGatewayMock class in this
codebase to import instead (P1 doesn't have one either; see PM-08's own
research notes).
"""

from __future__ import annotations

import json

import pytest
from spine.llm.gateway import LLMResponse

from pm.reporting.facts import BlockerFact, ItemFact, MorningBriefFacts, PersonFacts, SprintScopeFacts
from pm.reporting.morning_brief import GroundingError, generate_morning_brief


class FakeGateway:
    """Returns each of `responses` in order, one per call. Records every
    rendered prompt it was given (`.prompts`) and how many times it was
    called (`.calls`) -- the same two things P1's own daily-summary tests
    assert on instead of the model's actual wording."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.prompts.append(prompt)
        self.calls += 1
        text = self._responses.pop(0)
        return LLMResponse(
            text=text,
            provider="fake",
            model="fake",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0.0,
            cache_hit=False,
        )


def _facts() -> MorningBriefFacts:
    """A small, hand-built MorningBriefFacts -- deliberately not built
    from a real snapshot here, so this file's own assertions depend only
    on pm.reporting.morning_brief, not on the seed data's own shape."""
    return MorningBriefFacts(
        as_of="2026-09-16T23:59:59+00:00",
        sprint=SprintScopeFacts(
            sprint_id="sprint-13",
            display_name="Sprint 13",
            start_date="2026-09-07",
            end_date="2026-09-20",
            day_number=10,
            total_days=14,
            total_items=13,
            done_items=4,
        ),
        people=[
            PersonFacts(
                assignee_id="wei.chen",
                committed=[],
                delivered=[],
                pending=[],
                blocked=[
                    ItemFact(
                        item_id="PM-023",
                        title="Auth flow token refresh failing intermittently in staging",
                    )
                ],
            )
        ],
        blockers=[
            BlockerFact(
                risk_id="RISK-001",
                title="Auth flow token refresh intermittent failures in staging",
                severity="medium",
                related_item_id="PM-023",
                assignee_id="wei.chen",
            )
        ],
    )


def _tool_response(narrative: str) -> str:
    return json.dumps({"narrative": narrative})


def test_regenerating_the_same_facts_yields_identical_facts_but_wording_may_differ():
    """The row's own acceptance test, run end to end through the model
    call: same facts in, twice; .facts identical both times, .narrative
    allowed to differ."""
    facts = _facts()
    gateway = FakeGateway(
        [
            _tool_response("Day 10 of Sprint 13, 4 of 13 items done. wei.chen is blocked on PM-023 (RISK-001)."),
            _tool_response("It is day 10 of 14 in Sprint 13; 4 of 13 items are done. wei.chen remains stuck on PM-023, tracked as RISK-001."),
        ]
    )

    first = generate_morning_brief(facts, gateway)
    second = generate_morning_brief(facts, gateway)

    assert first.facts == second.facts
    assert first.facts == facts
    assert first.narrative != second.narrative
    assert gateway.calls == 2


def test_a_narrative_that_invents_an_item_id_is_rejected_and_retried():
    """PM-999 does not exist anywhere in these facts -- the first
    response must be rejected and a second attempt made with the
    mismatch fed back."""
    facts = _facts()
    gateway = FakeGateway(
        [
            _tool_response("wei.chen is also blocked on PM-999, which was never mentioned above."),
            _tool_response("wei.chen is blocked on PM-023 (RISK-001)."),
        ]
    )

    result = generate_morning_brief(facts, gateway)

    assert gateway.calls == 2
    assert "PM-999" not in result.narrative
    # the retry's own feedback must have reached the model
    assert "PM-999" in gateway.prompts[1]


def test_a_narrative_that_never_recovers_raises_grounding_error():
    facts = _facts()
    gateway = FakeGateway([_tool_response("See PM-999 for details.")] * 3)

    with pytest.raises(GroundingError):
        generate_morning_brief(facts, gateway, max_attempts=3)

    assert gateway.calls == 3


def test_a_narrative_that_invents_a_risk_id_is_also_rejected():
    facts = _facts()
    gateway = FakeGateway(
        [
            _tool_response("Also see RISK-404, a brand new risk."),
            _tool_response("wei.chen is blocked on PM-023 (RISK-001)."),
        ]
    )

    result = generate_morning_brief(facts, gateway)
    assert "RISK-404" not in result.narrative
    assert gateway.calls == 2
