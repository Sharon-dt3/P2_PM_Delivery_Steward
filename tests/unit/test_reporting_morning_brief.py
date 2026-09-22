"""PM-08 (expression) + PM-09 (reference-or-drop grounding) + PM-10
(honest absence).

This module's own row-level acceptance tests, all three literal:

  PM-08: "Regenerating on the same snapshot yields identical facts;
  wording may differ."
  PM-09: "A hand-forged unreferenced line is dropped and logged."
  PM-10: "The zero-activity assignee appears with an explicit no-update
  line."

No real network call happens anywhere in this file. FakeGateway mirrors
P1's own testing convention exactly (see e.g.
../P3_Agents/tests/unit/test_daily_summary.py): a duck-typed stand-in
matching LLMGateway's own `.generate(prompt, **kwargs) -> LLMResponse`
shape, not a real HTTP call -- there is no LLMGatewayMock class in this
codebase to import instead (P1 doesn't have one either).

The facts fixture below is deliberately minimal (one person, one item,
one blocker, one sprint) so each test can hand-author exactly the model
responses it needs, in the exact section call order generate_morning_brief
uses (sprint_scope, committed, delivered, pending, blocked, blockers --
skipping any section with no facts, calling the model zero times for
those, same as P1's own empty-section rule).
"""

from __future__ import annotations

import json

from spine.llm.gateway import LLMResponse

from pm.adapters.commitments import Commitment
from pm.reporting.facts import BlockerFact, ItemFact, MorningBriefFacts, PersonFacts, SprintScopeFacts
from pm.reporting.morning_brief import generate_morning_brief


class FakeGateway:
    """Returns each of `responses` in order, one per call. Records every
    rendered prompt it was given (`.prompts`) and how many times it was
    called (`.calls`)."""

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
    """One person (wei.chen), blocked on exactly one item (PM-023), with
    no commitments/delivered/pending -- so only three sections ever call
    the model: sprint_scope, blocked, blockers."""
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
                blocked=[ItemFact(item_id="PM-023", title="Auth flow token refresh failing intermittently in staging")],
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


def _lines_response(*lines: tuple[str, str | None]) -> str:
    """lines: (text, reference_id) pairs."""
    return json.dumps({"lines": [{"text": text, "reference_id": ref} for text, ref in lines]})


def test_regenerating_the_same_facts_yields_identical_facts_but_content_may_differ():
    """PM-08's own acceptance test, still true after PM-09's redesign:
    same facts in, twice; .facts identical both times, .content allowed
    to differ."""
    facts = _facts()

    def responses():
        return [
            _lines_response(("Day 10 of 14 in Sprint 13, 4 of 13 items done.", "sprint:sprint-13")),
            _lines_response(("wei.chen is blocked on PM-023.", "item:PM-023")),
            _lines_response(("RISK-001 (medium): auth token refresh failures, tied to PM-023.", "risk:RISK-001")),
        ]

    def responses_reworded():
        return [
            _lines_response(("It's day 10 of 14 in Sprint 13; 4 of 13 items are done.", "sprint:sprint-13")),
            _lines_response(("wei.chen remains stuck on PM-023.", "item:PM-023")),
            _lines_response(("Medium-severity RISK-001 is still open, linked to PM-023.", "risk:RISK-001")),
        ]

    first = generate_morning_brief(facts, FakeGateway(responses()))
    second = generate_morning_brief(facts, FakeGateway(responses_reworded()))

    assert first.facts == second.facts
    assert first.facts == facts
    assert first.content != second.content


def test_an_empty_section_never_calls_the_model():
    """committed/delivered/pending are all empty in this fixture -- only
    sprint_scope, blocked and blockers should ever reach the gateway."""
    facts = _facts()
    gateway = FakeGateway(
        [
            _lines_response(("Day 10 of 14 in Sprint 13, 4 of 13 items done.", "sprint:sprint-13")),
            _lines_response(("wei.chen is blocked on PM-023.", "item:PM-023")),
            _lines_response(("RISK-001 (medium): auth token refresh failures, tied to PM-023.", "risk:RISK-001")),
        ]
    )
    generate_morning_brief(facts, gateway)
    assert gateway.calls == 3


def test_a_hand_forged_line_with_no_reference_is_dropped_and_logged(caplog):
    """PM-09's own acceptance test, literal: "A hand-forged unreferenced
    line is dropped and logged." The blockers section's only line is
    hand-forged with reference_id=None -- never something any real
    fact-rendering in this module would produce -- and the same bad
    response is returned on every retry attempt, so it never recovers."""
    facts = _facts()
    bad_blocker_response = _lines_response(("A blocker exists, trust me.", None))

    gateway = FakeGateway(
        [
            _lines_response(("Day 10 of 14 in Sprint 13, 4 of 13 items done.", "sprint:sprint-13")),
            _lines_response(("wei.chen is blocked on PM-023.", "item:PM-023")),
            bad_blocker_response,  # blockers, attempt 1
            bad_blocker_response,  # blockers, attempt 2
            bad_blocker_response,  # blockers, attempt 3 -- gives up here
        ]
    )

    with caplog.at_level("WARNING", logger="spine.grounding.kernel"):
        brief = generate_morning_brief(facts, gateway)

    # dropped, not rendered
    assert "trust me" not in brief.content
    assert brief.sections["blockers"] == []

    # dropped AND logged, with the reason the kernel itself assigns
    assert len(brief.dropped["blockers"]) == 1
    assert brief.dropped["blockers"][0]["reason"] == "unresolvable_message_id"
    assert "trust me" in brief.dropped["blockers"][0]["text"]
    assert any("grounding_dropped" in record.message for record in caplog.records)


def test_a_line_citing_an_unknown_reference_id_is_also_dropped_and_logged(caplog):
    """Not just a missing reference -- one that doesn't resolve at all
    (a risk id nothing in these facts has) fails the exact same way."""
    facts = _facts()
    bad_blocker_response = _lines_response(("See RISK-404 for details.", "risk:RISK-404"))

    gateway = FakeGateway(
        [
            _lines_response(("Day 10 of 14 in Sprint 13, 4 of 13 items done.", "sprint:sprint-13")),
            _lines_response(("wei.chen is blocked on PM-023.", "item:PM-023")),
            bad_blocker_response,
            bad_blocker_response,
            bad_blocker_response,
        ]
    )

    with caplog.at_level("WARNING", logger="spine.grounding.kernel"):
        brief = generate_morning_brief(facts, gateway)

    assert "RISK-404" not in brief.content
    assert brief.dropped["blockers"][0]["reason"] == "unresolvable_message_id"
    assert any("grounding_dropped" in record.message for record in caplog.records)


def test_grounding_retries_before_giving_up():
    """A bad first attempt followed by a good one on retry must recover
    -- not every ungrounded response is a dead end, only one that never
    corrects itself within the retry budget."""
    facts = _facts()
    gateway = FakeGateway(
        [
            _lines_response(("Day 10 of 14 in Sprint 13, 4 of 13 items done.", "sprint:sprint-13")),
            _lines_response(("wei.chen is blocked on PM-023.", "item:PM-023")),
            _lines_response(("See RISK-404 for details.", "risk:RISK-404")),  # blockers, attempt 1: bad
            _lines_response(("RISK-001 (medium): auth token refresh failures.", "risk:RISK-001")),  # attempt 2: good
        ]
    )

    brief = generate_morning_brief(facts, gateway)

    assert len(brief.sections["blockers"]) == 1
    assert brief.dropped["blockers"] == []
    assert "RISK-001" in brief.content
    # the retry's own feedback (naming the failed reference) must have reached the model
    assert "RISK-404" in gateway.prompts[-1]


def test_committed_facts_can_ground_on_the_commitment_id_when_the_item_id_is_absent():
    """A commitment with no item_id still has SOMETHING real to cite:
    its own commitment id."""
    facts = MorningBriefFacts(
        as_of="2026-09-16T23:59:59+00:00",
        sprint=None,
        people=[
            PersonFacts(
                assignee_id="mateo.silva",
                committed=[
                    Commitment(
                        id=42,
                        member_id="mateo.silva",
                        item_id=None,
                        text="I'll have this fixed by end of week.",
                        due_date_iso=None,
                        due_date_text="end of week",
                        made_at="2026-09-15",
                        source_message_id=None,
                    )
                ],
                delivered=[],
                pending=[],
                blocked=[],
            )
        ],
        blockers=[],
    )
    gateway = FakeGateway(
        [_lines_response(('mateo.silva committed: "I\'ll have this fixed by end of week."', "commitment:42"))]
    )

    brief = generate_morning_brief(facts, gateway)

    assert brief.dropped["committed"] == []
    assert len(brief.sections["committed"]) == 1
    assert "mateo.silva" in brief.content


def _person_block(content: str, assignee_id: str) -> str:
    """The rendered lines for exactly one person's own "## <id>" heading,
    up to (not including) the next "## " heading or the end of the
    content -- so a test can check what a person's own block says
    without being tripped up by another person's or the blockers
    section's lines."""
    after_heading = content.split(f"## {assignee_id}\n", 1)[1]
    return after_heading.split("\n## ", 1)[0]


def test_the_zero_activity_assignee_appears_with_an_explicit_no_update_line():
    """PM-10's own acceptance test, literal: "The zero-activity assignee
    appears with an explicit no-update line." sofia.lindqvist owns no
    committed/delivered/pending/blocked fact and authored no commit
    (commit_count=0) -- has_activity is False -- while wei.chen, in the
    same brief, has one delivered item and gets the normal per-bucket
    rendering. No model call happens for sofia at all: she contributes
    zero facts to every section, and her line is rendered directly by
    _render_brief, not generated -- see morning_brief.py's own
    docstring on why an absence needs no prose."""
    facts = MorningBriefFacts(
        as_of="2026-09-16T23:59:59+00:00",
        sprint=None,
        people=[
            PersonFacts(
                assignee_id="wei.chen",
                committed=[],
                delivered=[ItemFact(item_id="PM-002", title="Implement caching layer eviction policy")],
                pending=[],
                blocked=[],
            ),
            PersonFacts(
                assignee_id="sofia.lindqvist",
                committed=[],
                delivered=[],
                pending=[],
                blocked=[],
                commit_count=0,
            ),
        ],
        blockers=[],
    )
    assert facts.people[1].has_activity is False

    gateway = FakeGateway([_lines_response(("Wei Chen delivered PM-002.", "item:PM-002"))])

    brief = generate_morning_brief(facts, gateway)

    # only wei.chen's one delivered fact ever reaches the model; sofia's
    # zero-activity line is never sent to it, and never dropped/logged
    # either -- there is nothing ungrounded about it, there is simply
    # nothing to say.
    assert gateway.calls == 1

    sofia_block = _person_block(brief.content, "sofia.lindqvist")
    assert sofia_block.strip() == "- No update: no tracker activity or commits recorded."
    assert "none." not in sofia_block  # the old four-bucket rendering, not this one

    wei_block = _person_block(brief.content, "wei.chen")
    assert "Wei Chen delivered PM-002." in wei_block
    assert "No update" not in wei_block
