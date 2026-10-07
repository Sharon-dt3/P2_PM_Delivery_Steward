"""A faithful stand-in for the model in end-of-day summary tests and offline runs: one line per fact in
the prompt, the reference echoed exactly and the fact's own text as the prose. `tamper` forges the
lines (a hallucination, a wrong reference); by default only the first attempt of each section is forged,
as a model correcting itself on retry, and `persistent=True` forges every attempt, as one that never does."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from spine.llm.gateway import LLMResponse

_SECTION_RE = re.compile(r'the "(?P<label>[^"]+)" section')
_FACT_RE = re.compile(r"^\d+\. reference_id: (?P<ref>\S+)\n\s+detail: (?P<detail>.*)$", re.MULTILINE)


class ScriptedSummaryGateway:
    def __init__(self, tamper: Callable[[list[dict]], list[dict]] | None = None, persistent: bool = False) -> None:
        self._tamper = tamper
        self._persistent = persistent
        self._attempts: dict[str, int] = {}
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        match = _SECTION_RE.search(prompt)
        key = match.group("label") if match else "unknown"
        self._attempts[key] = self._attempts.get(key, 0) + 1
        facts = prompt.split("Items:", 1)[-1]
        lines = [{"text": m.group("detail"), "reference_id": m.group("ref"), "quote": m.group("detail")} for m in _FACT_RE.finditer(facts)]
        if self._tamper and (self._attempts[key] == 1 or self._persistent):
            lines = self._tamper(lines)
        return LLMResponse(text=json.dumps({"lines": lines}), provider="scripted", model="scripted", prompt_tokens=0,
                           completion_tokens=0, latency_ms=0.0, cache_hit=False)
