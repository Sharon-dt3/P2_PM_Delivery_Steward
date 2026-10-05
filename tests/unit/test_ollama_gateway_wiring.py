"""The gateway's Ollama path loads a prompt (`ollama_schema_instructions`)
from the consuming repo's own prompts/ directory. P1 has it; P2 did not,
so every structured call through a local model crashed with
PromptNotFoundError before reaching the model -- found by the first live
run of the morning brief against Ollama (2026-10-05).

No real Ollama server is needed: the HTTP client is stubbed, so this only
proves P2 supplies what the gateway needs to build and send the request,
and that a well-formed reply parses into the brief's own schema."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from pm.reporting.morning_brief import MorningBriefSectionDraft
from spine.llm.gateway import LLMGateway
from spine.llm.structured import generate_structured

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_a_structured_call_through_the_ollama_provider_works_from_this_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)  # the gateway resolves prompts/ relative to the working directory
    seen_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_prompts.append(json.loads(request.content)["prompt"])
        reply = {"lines": [{"text": "Sprint 13 is on day 9 of 14.", "reference_id": "sprint:sprint-13"}]}
        return httpx.Response(200, json={"response": json.dumps(reply)})

    gateway = LLMGateway(
        provider="ollama", cache_dir=tmp_path / "cache", call_log_path=tmp_path / "calls.jsonl",
    )
    gateway._make_ollama_client = lambda *a, **k: httpx.Client(
        base_url="http://localhost:11434", transport=httpx.MockTransport(handler)
    )

    draft = generate_structured(
        gateway, "Write the sprint line.", MorningBriefSectionDraft, tool_name="morning_brief_section"
    )

    assert draft.lines[0].reference_id == "sprint:sprint-13"
    # The model was actually told the schema it must satisfy, not just the bare prompt.
    assert "reference_id" in seen_prompts[0] and "Write the sprint line." in seen_prompts[0]
