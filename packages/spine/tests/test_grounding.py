"""
Real proof of spine.grounding.kernel standalone -- the module's own
docstring already claims independence from Teams/messages-tables/any
agent-specific type; this proves it by running it against a lookup
function backed by nothing more than a plain dict.
"""
from __future__ import annotations

from spine.grounding.kernel import FactualLine, verify_line, verify_lines

MESSAGES = {
    "msg-1": "We shipped the migration to production this morning.",
    "msg-2": "Blocked on the vendor API key, waiting on procurement.",
}


def lookup(message_id: str) -> str | None:
    return MESSAGES.get(message_id)


def test_line_with_resolvable_id_and_no_quote_passes():
    line = FactualLine(text="The migration shipped.", message_id="msg-1", quote=None)
    assert verify_line(line, lookup) is None


def test_line_with_unresolvable_id_fails():
    line = FactualLine(text="Something happened.", message_id="msg-does-not-exist", quote=None)
    failure = verify_line(line, lookup)
    assert failure is not None
    assert "unresolvable_message_id" in failure.reason


def test_line_with_missing_id_fails():
    line = FactualLine(text="Something happened.", message_id=None, quote=None)
    failure = verify_line(line, lookup)
    assert failure is not None


def test_line_with_verbatim_quote_that_matches_passes():
    line = FactualLine(
        text="They shipped the migration.", message_id="msg-1",
        quote="shipped the migration to production",
    )
    assert verify_line(line, lookup) is None


def test_line_with_quote_that_does_not_match_verbatim_fails():
    line = FactualLine(
        text="They shipped the migration.", message_id="msg-1",
        quote="deployed the migration to prod",  # not a literal substring
    )
    failure = verify_line(line, lookup)
    assert failure is not None


def test_verify_lines_drops_only_the_failing_lines():
    lines = [
        FactualLine(text="Good line.", message_id="msg-1", quote=None),
        FactualLine(text="Bad line.", message_id="msg-nope", quote=None),
        FactualLine(text="Blocked line.", message_id="msg-2", quote="Blocked on the vendor API key"),
    ]
    result = verify_lines(lines, lookup)
    assert len(result.grounded_lines) == 2
    assert len(result.failures) == 1
    assert result.failures[0].line.text == "Bad line."
