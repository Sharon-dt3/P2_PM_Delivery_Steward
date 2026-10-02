"""
Structured-output layer (SPN-03): forced-schema generation with
validate-and-retry. A capability calls generate_structured(...) instead
of talking to the gateway directly whenever it needs a typed, validated
response — never a silent default on failure.
"""

from __future__ import annotations

import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("spine.llm.structured")

ModelT = TypeVar("ModelT", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """Raised when no valid instance of the schema was produced within the
    allowed attempts. The caller must handle this -- there is no default."""


def _unwrap_schema_echo(payload, schema: type[ModelT]):
    """Weak/local models (see gateway._call_ollama's own docstring --
    Ollama has no native tool-calling API, so the raw JSON Schema is
    the only shape hint they get) can echo that schema's own
    "properties" wrapper back with real values spliced into the
    leaves, instead of returning a flat instance -- e.g.
    {"properties": {"narrative": "..."}} instead of
    {"narrative": "..."}. Live-observed tonight on WeeklyNarrativeDraft
    even after gateway.py's prompt was improved to show a concrete
    example instance: the fix reduced it from every attempt failing to
    a later retry succeeding, but did not stop attempt 1 from still
    doing this sometimes. Repairing it here, once, with certainty, is
    strictly better than spending a whole retry attempt (a fresh model
    call) on a shape this codebase can already recognize and fix for
    free. "properties" is never itself one of this codebase's real
    schema field names, so the unwrap below is unambiguous -- a
    correctly-shaped payload is always returned untouched."""
    if not isinstance(payload, dict):
        return payload
    field_names = set(schema.model_fields.keys())
    if field_names & payload.keys():
        return payload  # already flat (or partially flat) -- leave it alone
    inner = payload.get("properties")
    if isinstance(inner, dict) and (field_names & inner.keys()):
        return inner
    return payload


def generate_structured(
    gateway,
    prompt: str,
    schema: type[ModelT],
    *,
    system: str | None = None,
    tool_name: str | None = None,
    max_attempts: int = 3,
    max_tokens: int = 1024,
) -> ModelT:
    tool_name = tool_name or schema.__name__.lower()
    tool = {
        "name": tool_name,
        "description": f"Return a {schema.__name__} matching the given schema.",
        "input_schema": schema.model_json_schema(),
    }
    tool_choice = {"type": "tool", "name": tool_name}

    current_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        response = gateway.generate(
            current_prompt,
            system=system,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice=tool_choice,
            skip_cache=(attempt > 1),
        )
        try:
            payload = json.loads(response.text)
            payload = _unwrap_schema_echo(payload, schema)
            instance = schema.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning(
                "structured_output_retry attempt=%d schema=%s error=%s",
                attempt, schema.__name__, exc,
            )
            current_prompt = (
                f"{prompt}\n\nYour previous response was invalid: {exc}\n"
                f"Return ONLY a valid call to {tool_name} matching its schema exactly."
            )
            continue
        else:
            return instance

    raise StructuredOutputError(
        f"{schema.__name__}: no valid response after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )
