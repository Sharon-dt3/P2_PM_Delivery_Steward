"""Grounding used to check only that a brief line cited a real fact in the
right section, never that its WORDS were true: "Aisha shipped PM-009 ahead
of schedule, closed 12 bugs and got praise from the client", citing a
delivered item that says none of that, went straight into the brief.

Two deterministic checks now stand between the model and the brief, both run
inside spine's retry-with-feedback loop:

  1. every line must carry a `quote`: an exact fragment (at least 8
     characters) of the fact it cites, which the kernel verifies verbatim;
  2. every item/risk id and every number in a line's text must appear in the
     fact it cites -- compared by value, so "7" matches "2026-09-07".

What this does NOT catch is embellishment made of ordinary words around a
valid quote (see the last test). That limit is real and is pinned there.
"""

from __future__ import annotations

import json
import logging

from spine.llm.gateway import LLMResponse

from pm.reporting.facts import (
    ItemFact,
    MorningBriefFacts,
    PersonFacts,
    SprintScopeFacts,
)
from pm.reporting.morning_brief import generate_morning_brief

DELIVERED_QUOTE = "Ship login page"
SPRINT_DETAIL_QUOTE = "Sprint 13"


class QueuedGateway:
    """Returns each queued reply in turn and keeps every prompt it was given."""

    def __init__(self, replies: list[list[dict]]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.prompts.append(prompt)
        lines = self._replies.pop(0)
        return LLMResponse(
            text=json.dumps({"lines": lines}), provider="fake", model="fake",
            prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False,
        )


def _facts(sprint: SprintScopeFacts | None = None) -> MorningBriefFacts:
    """One person with one delivered item and nothing else, so exactly one
    section (delivered) ever calls the model -- or two with a sprint."""
    return MorningBriefFacts(
        as_of="2026-09-16T23:59:59+00:00",
        sprint=sprint,
        people=[
            PersonFacts(
                assignee_id="aisha.rahman", committed=[],
                delivered=[ItemFact(item_id="PM-001", title="Ship login page")],
                pending=[], blocked=[],
            )
        ],
        blockers=[],
    )


def _line(text, quote=DELIVERED_QUOTE, ref="item:PM-001"):
    line = {"text": text, "reference_id": ref}
    if quote is not None:
        line["quote"] = quote
    return line


FAITHFUL = _line("Aisha delivered the login page.")
INVENTED = _line("Aisha shipped PM-009 ahead of schedule and closed 12 bugs.")


def test_a_line_with_invented_ids_and_numbers_is_dropped_and_logged(caplog):
    gateway = QueuedGateway([[INVENTED], [INVENTED], [INVENTED]])  # the model never corrects itself

    with caplog.at_level(logging.WARNING, logger="spine.grounding.kernel"):
        brief = generate_morning_brief(_facts(), gateway)

    assert brief.sections["delivered"] == []
    assert "12 bugs" not in brief.content and "PM-009" not in brief.content
    (failure,) = brief.dropped["delivered"]
    assert failure["reason"] == "content_not_supported"
    assert "PM-009" in failure["detail"] and "12" in failure["detail"]
    assert any("grounding_dropped" in r.message and "content_not_supported" in r.message for r in caplog.records)


def test_the_problem_is_fed_back_and_a_corrected_line_is_kept():
    gateway = QueuedGateway([[INVENTED], [FAITHFUL]])

    brief = generate_morning_brief(_facts(), gateway)

    assert [line.text for line in brief.sections["delivered"]] == ["Aisha delivered the login page."]
    assert "content_not_supported" in gateway.prompts[1] and "PM-009" in gateway.prompts[1]


def test_a_line_with_no_quote_is_rejected_then_accepted_once_it_quotes():
    gateway = QueuedGateway([[_line("Aisha delivered the login page.", quote=None)], [FAITHFUL]])

    brief = generate_morning_brief(_facts(), gateway)

    assert len(brief.sections["delivered"]) == 1
    assert "quote" in gateway.prompts[1].lower()


def test_a_quote_that_is_not_a_verbatim_fragment_is_rejected():
    gateway = QueuedGateway([[_line("Aisha delivered it.", quote="Ship the login pages")]] * 3)

    brief = generate_morning_brief(_facts(), gateway)

    assert brief.sections["delivered"] == []
    assert brief.dropped["delivered"][0]["reason"] == "quote_not_verbatim"


def test_a_token_quote_is_too_short_to_count():
    gateway = QueuedGateway([[_line("Aisha delivered it.", quote="Ship")]] * 3)

    brief = generate_morning_brief(_facts(), gateway)

    assert brief.sections["delivered"] == []
    assert brief.dropped["delivered"][0]["reason"] == "content_not_supported"


def test_numbers_and_dates_are_compared_by_value_not_by_spelling():
    sprint = SprintScopeFacts(
        sprint_id="sprint-13", display_name="Sprint 13", start_date="2026-09-07", end_date="2026-09-20",
        day_number=10, total_days=14, total_items=13, done_items=4,
    )
    ok = {
        "text": "Sprint 13 runs from 7 September to 20 September: day 10 of 14, with 4 of 13 items done.",
        "reference_id": "sprint:sprint-13", "quote": SPRINT_DETAIL_QUOTE + " (sprint-13)",
    }
    wrong = {**ok, "text": "Sprint 13 is day 10 of 14, with 11 of 13 items done."}

    good = generate_morning_brief(_facts(sprint), QueuedGateway([[ok], [FAITHFUL]]))
    bad = generate_morning_brief(_facts(sprint), QueuedGateway([[wrong]] * 3 + [[FAITHFUL]]))

    assert len(good.sections["sprint_scope"]) == 1
    assert bad.sections["sprint_scope"] == []
    assert "11" in bad.dropped["sprint_scope"][0]["detail"]


def test_an_honest_paraphrase_with_no_new_specifics_passes():
    brief = generate_morning_brief(
        _facts(), QueuedGateway([[_line("It was Aisha who delivered the login page.")]])
    )
    assert len(brief.sections["delivered"]) == 1


def test_known_limit_embellishment_in_plain_words_around_a_valid_quote_still_passes():
    """Pinned on purpose. With no id or number to compare, a claim such as
    "got praise from the client" cannot be caught by these two checks. If a
    stricter check is ever added, this test is the one to change."""
    brief = generate_morning_brief(
        _facts(), QueuedGateway([[_line("Aisha delivered the login page and got praise from the client.")]])
    )
    assert len(brief.sections["delivered"]) == 1
