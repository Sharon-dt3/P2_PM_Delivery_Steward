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


# --- content_check: an optional third check a caller can add -----------------
# (added in P2's copy of spine; see PROVENANCE.md). The kernel stays generic:
# it only knows that a check returns None (fine) or a reason string.

from spine.grounding.kernel import ground_with_retry


def _no_digits_beyond_the_source(line: FactualLine, resolved_text: str) -> str | None:
    extra = [t for t in line.text.split() if t.isdigit() and t not in resolved_text]
    return f"mentions {extra} which the source does not" if extra else None


def test_a_content_check_can_reject_a_line_that_resolves_and_quotes_fine():
    line = FactualLine(text="We shipped it in 12 regions.", message_id="msg-1", quote=None)
    failure = verify_line(line, lookup, _no_digits_beyond_the_source)
    assert failure is not None
    assert failure.reason == "content_not_supported"
    assert "12" in failure.detail


def test_a_content_check_that_returns_nothing_changes_nothing():
    line = FactualLine(text="The migration shipped.", message_id="msg-1", quote=None)
    assert verify_line(line, lookup, _no_digits_beyond_the_source) is None
    assert verify_line(line, lookup) is None  # and it is optional


def test_the_content_check_is_not_consulted_when_the_reference_does_not_resolve():
    calls = []

    def spy(line, resolved_text):
        calls.append(line)

    verify_line(FactualLine(text="x", message_id="nope", quote=None), lookup, spy)
    verify_line(FactualLine(text="x", message_id=None, quote=None), lookup, spy)
    assert calls == []


def test_a_content_failure_is_fed_back_and_a_corrected_line_is_kept():
    attempts = []

    def generate(feedback):
        attempts.append(feedback)
        text = "We shipped the migration." if feedback else "We shipped the migration in 12 regions."
        return [FactualLine(text=text, message_id="msg-1", quote=None)]

    result = ground_with_retry(generate, lookup, content_check=_no_digits_beyond_the_source)

    assert [line.text for line in result.grounded_lines] == ["We shipped the migration."]
    assert attempts[0] is None
    assert "content_not_supported" in attempts[1] and "12" in attempts[1]
