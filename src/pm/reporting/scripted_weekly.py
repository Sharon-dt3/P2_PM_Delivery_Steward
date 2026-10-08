"""A faithful stand-in for the model in the weekly report's tests and offline runs (PM-29): for the narrative lines it echoes each fact exactly as the
end-of-day stand-in does, and for the closing sentence it writes one from the facts present, using only words the grounding rules allow. `tamper`
forges the lines the way the other stand-in does; `closing` replaces the sentence (to try a rule-breaking one)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from spine.llm.gateway import LLMResponse

from pm.reporting.scripted_summary import ScriptedSummaryGateway

_DETAIL_RE = re.compile(r"^\s+detail: (?P<detail>.*)$", re.MULTILINE)


def closing_from(prompt: str) -> str:
    details = " ".join(m.group("detail") for m in _DETAIL_RE.finditer(prompt.split("Facts:", 1)[-1]))
    clauses = []
    if "Completed this week" in details:
        move = "up" if " up " in f" {details} " else "down" if " down " in f" {details} " else "unchanged"
        clauses.append(f"Completed items are {move} against the week before")
    if "added after planning" in details:
        clauses.append("items were added after planning")
    if " is blocked" in details:
        clauses.append("items are blocked")
    if not clauses:
        return "Nothing else changed."
    return (clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + f", and {clauses[-1]}") + "."


class ScriptedWeeklyGateway:
    def __init__(self, tamper: Callable[[list[dict]], list[dict]] | None = None, persistent: bool = False,
                 closing: Callable[[int], str] | None = None) -> None:
        self._lines = ScriptedSummaryGateway(tamper=tamper, persistent=persistent)
        self._closing = closing  # attempt number (1-based) -> the sentence to return, instead of the faithful one
        self._closing_attempts = 0
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        if "closing sentence" in prompt:
            self._closing_attempts += 1
            sentence = self._closing(self._closing_attempts) if self._closing else closing_from(prompt)
            return LLMResponse(text=json.dumps({"sentence": sentence}), provider="scripted", model="scripted", prompt_tokens=0,
                               completion_tokens=0, latency_ms=0.0, cache_hit=False)
        return self._lines.generate(prompt, **kwargs)
