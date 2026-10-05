"""When a generated line is dropped for good, the brief must not say
something false in its place. It used to write "Sprint scope: no sprint on
file covers this date." when the sprint existed and only its line had failed
grounding, and "- Delivered: none." for a person who had delivered items.
Now a fact whose line was dropped is shown as the fact itself, marked as
recorded; "none." / "no sprint on file" stay for when there really is nothing.
"""

from __future__ import annotations

import json

from spine.llm.gateway import LLMResponse

from pm.reporting.facts import (
    BlockerFact,
    ItemFact,
    MorningBriefFacts,
    PersonFacts,
    SprintScopeFacts,
)
from pm.reporting.morning_brief import generate_morning_brief

MARK = "[as recorded]"
BAD = {"text": "Totally invented content with 99 numbers.", "reference_id": None}


class Queued:
    def __init__(self, replies):
        self._replies = list(replies)

    def generate(self, prompt, **kwargs):
        lines = self._replies.pop(0)
        return LLMResponse(
            text=json.dumps({"lines": lines}), provider="fake", model="fake",
            prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False,
        )


SPRINT = SprintScopeFacts(
    sprint_id="sprint-13", display_name="Sprint 13", start_date="2026-09-07", end_date="2026-09-20",
    day_number=9, total_days=14, total_items=17, done_items=3,
)


def _facts(sprint=SPRINT, blockers=()):
    return MorningBriefFacts(
        as_of="2026-09-15T23:59:59+00:00", sprint=sprint,
        people=[PersonFacts(
            assignee_id="aisha.rahman", committed=[],
            delivered=[ItemFact(item_id="PM-001", title="Ship login page")], pending=[], blocked=[],
        )],
        blockers=list(blockers),
    )


GOOD_DELIVERED = {"text": "Aisha delivered the login page.", "reference_id": "item:PM-001", "quote": "Ship login page"}


def test_a_dropped_sprint_line_does_not_claim_there_is_no_sprint():
    brief = generate_morning_brief(_facts(), Queued([[BAD]] * 3 + [[GOOD_DELIVERED]]))

    first = brief.content.splitlines()[0]
    assert "no sprint on file" not in brief.content
    assert "Sprint 13 (sprint-13), day 9 of 14" in first and MARK in first


def test_when_there_really_is_no_sprint_it_still_says_so():
    brief = generate_morning_brief(_facts(sprint=None), Queued([[GOOD_DELIVERED]]))

    assert brief.content.splitlines()[0] == "Sprint scope: no sprint on file covers this date."


def test_a_bucket_whose_line_was_dropped_shows_the_fact_not_none():
    brief = generate_morning_brief(_facts(sprint=None), Queued([[BAD]] * 3))

    assert "- Delivered: none." not in brief.content
    assert f"- Delivered: aisha.rahman: PM-001 (Ship login page). {MARK}" in brief.content
    assert "- Pending: none." in brief.content  # nothing to drop there: this one really is empty


def test_a_blocker_whose_line_was_dropped_is_still_listed():
    blocker = BlockerFact(risk_id="RISK-001", title="Token refresh failing", severity="medium",
                          related_item_id="PM-001", assignee_id="aisha.rahman")

    brief = generate_morning_brief(_facts(sprint=None, blockers=[blocker]), Queued([[GOOD_DELIVERED]] + [[BAD]] * 3))

    blockers_section = brief.content.split("## Blockers")[1]
    assert "- none." not in blockers_section
    assert "RISK-001" in blockers_section and MARK in blockers_section


def test_no_marker_appears_when_every_line_grounded():
    brief = generate_morning_brief(_facts(), Queued([[{
        "text": "Sprint 13 is on day 9 of 14, with 3 of 17 items done.",
        "reference_id": "sprint:sprint-13", "quote": "Sprint 13 (sprint-13)",
    }], [GOOD_DELIVERED]]))

    assert MARK not in brief.content
