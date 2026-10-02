"""
Grounding kernel: reference-or-drop and verbatim quote verifier (SPN-06).

Shared across all three agents (P1/P2/P3) -- deliberately independent
of Teams, messages tables, or any P1-specific type. It is handed a
lookup function from a message ID to that message's own literal stored
text, and a batch of factual lines a model produced, and it enforces
exactly one rule for each: a factual line that cannot be traced back to
a real message -- verbatim, wherever it claims a quote -- does not
survive into the caller's final output.

"The single highest-leverage artefact of the programme" per this task's
own WBS row: a fluent summary nobody can verify is worse than no
summary at all, because it will be believed regardless.

Two independent checks, per line:

1. reference-or-drop: the line must carry a message_id, and that
   message_id must actually resolve via the caller's message_lookup.
   No ID, or an ID nothing resolves to, is the same failure --
   "unresolvable_message_id".

2. verbatim quote: if (and only if) the line also claims a quote, that
   exact text must be a literal substring of the resolved message's own
   stored text -- case-sensitive, no fuzzy or near-miss matching. A
   line with no quote at all only has to clear check 1.

ground_with_retry is the orchestration half: "on failure, re-prompt
with the failure fed back, then drop and log" (this task's own WBS
wording). It re-calls the caller's generate_fn with a description of
exactly what failed, up to max_attempts times -- the same
retry-with-the-error-fed-back shape SPN-03's generate_structured uses
for schema validation, applied here to citation/quote correctness
instead. Whatever still fails to ground after the final attempt is
dropped from the result and logged; it is never silently kept, and the
kernel never invents a fix on the model's behalf.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel

logger = logging.getLogger("spine.grounding.kernel")

MessageLookup = Callable[[str], str | None]


class FactualLine(BaseModel):
    """One factual claim a model produced. text is the human-readable
    prose a caller will eventually render; message_id is the message
    this line claims to be grounded in; quote, if given, is a fragment
    the model claims appears verbatim in that message."""

    text: str
    message_id: str | None = None
    quote: str | None = None


@dataclass(frozen=True)
class GroundingFailure:
    line: FactualLine
    reason: str  # "unresolvable_message_id" | "quote_not_verbatim"
    detail: str


@dataclass(frozen=True)
class GroundingResult:
    grounded_lines: list[FactualLine] = field(default_factory=list)
    failures: list[GroundingFailure] = field(default_factory=list)


def verify_line(line: FactualLine, message_lookup: MessageLookup) -> GroundingFailure | None:
    """Check one line. Returns None if it passes, otherwise the
    GroundingFailure describing exactly why it doesn't."""
    if not line.message_id:
        return GroundingFailure(
            line=line,
            reason="unresolvable_message_id",
            detail="line carries no message_id at all",
        )

    resolved_text = message_lookup(line.message_id)
    if resolved_text is None:
        return GroundingFailure(
            line=line,
            reason="unresolvable_message_id",
            detail=f"message_id={line.message_id!r} does not resolve to any stored message",
        )

    if line.quote is not None and line.quote not in resolved_text:
        return GroundingFailure(
            line=line,
            reason="quote_not_verbatim",
            detail=f"quote {line.quote!r} is not a literal substring of message_id={line.message_id!r}",
        )

    return None


def verify_lines(lines: list[FactualLine], message_lookup: MessageLookup) -> GroundingResult:
    """Partition a batch of lines into what grounds and what doesn't.
    Does not retry or log anything itself -- that is
    ground_with_retry's job; this is the pure check callers can also
    use directly when they don't need the retry loop."""
    result = GroundingResult()
    for line in lines:
        failure = verify_line(line, message_lookup)
        if failure is None:
            result.grounded_lines.append(line)
        else:
            result.failures.append(failure)
    return result


def ground_with_retry(
    generate_fn: Callable[[str | None], list[FactualLine]],
    message_lookup: MessageLookup,
    *,
    max_attempts: int = 3,
) -> GroundingResult:
    """generate_fn(feedback) -> list[FactualLine]. Called with
    feedback=None on the first attempt; on every subsequent attempt,
    feedback is a human-readable description of exactly what failed
    last time, for the caller's own prompt to re-ask the model with.

    Regenerates the WHOLE batch on each retry (matching
    generate_structured's own re-prompt-the-whole-request shape) rather
    than asking the model to patch individual lines -- simpler, and
    consistent with how the rest of this codebase already retries model
    output. Whatever is still ungrounded after the final attempt is
    dropped and logged, never returned as if it had passed.
    """
    feedback: str | None = None
    result = GroundingResult()

    for attempt in range(1, max_attempts + 1):
        lines = generate_fn(feedback)
        result = verify_lines(lines, message_lookup)

        if not result.failures:
            return result

        if attempt < max_attempts:
            feedback = _describe_failures(result.failures)
            logger.warning(
                "grounding_retry attempt=%d of %d failures=%d",
                attempt, max_attempts, len(result.failures),
            )

    for failure in result.failures:
        logger.warning(
            "grounding_dropped message_id=%s reason=%s detail=%s text=%r",
            failure.line.message_id, failure.reason, failure.detail, failure.line.text,
        )

    return result


def _describe_failures(failures: list[GroundingFailure]) -> str:
    lines = [f"- {f.reason}: {f.detail} (line text: {f.line.text!r})" for f in failures]
    return (
        "The following factual line(s) failed grounding verification and must be "
        "fixed (or dropped from your response) before this can be accepted:\n"
        + "\n".join(lines)
    )
