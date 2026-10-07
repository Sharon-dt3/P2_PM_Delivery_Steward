"""A faithful stand-in for the model in PM-16 tests and offline runs: for the blocker a
prompt is about, it writes the description and impact straight from the evidence, with
the reference echoed and a verbatim quote -- the answer a good model gives. `rewrite`
forges its lines (a hallucination, a wrong reference); by default only the first attempt
is forged, as a model correcting itself on retry, and `persistent=True` forges every
attempt, as a model that never does."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from spine.llm.gateway import LLMResponse

from pm.risk.gaps import BlockerGap

_REFERENCE_RE = re.compile(r"reference_id: (item:[A-Za-z]+-\d+)")


class ScriptedRiskGateway:
    def __init__(
        self,
        gaps: list[BlockerGap],
        rewrite: Callable[[list[dict], BlockerGap], list[dict]] | None = None,
        persistent: bool = False,
    ) -> None:
        self._gaps = {g.reference: g for g in gaps}
        self._rewrite = rewrite
        self._persistent = persistent
        self._attempts: dict[str, int] = {}
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        match = _REFERENCE_RE.search(prompt)
        gap = self._gaps.get(match.group(1)) if match else None
        lines: list[dict] = []
        if gap is not None:
            attempt = self._attempts[gap.reference] = self._attempts.get(gap.reference, 0) + 1
            lines = [
                {"kind": "description", "text": gap.description_text(), "reference_id": gap.reference, "quote": gap.description_quote()},
                {"kind": "impact", "text": gap.impact_text(), "reference_id": gap.reference, "quote": gap.impact_quote()},
            ]
            if '"mitigation"' in prompt:  # the PM-19 prompt also asks for a mitigation line
                lines.append({"kind": "mitigation", "text": gap.mitigation_text(), "reference_id": gap.reference,
                              "quote": gap.mitigation_quote()})
            if self._rewrite and (attempt == 1 or self._persistent):
                lines = self._rewrite(lines, gap)
        return LLMResponse(
            text=json.dumps({"lines": lines}), provider="scripted", model="scripted", prompt_tokens=0, completion_tokens=0,
            latency_ms=0.0, cache_hit=False,
        )
