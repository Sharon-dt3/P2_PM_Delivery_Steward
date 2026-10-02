# spine

The shared internal package extracted at Gate G1 (CHN-33) from P1 Teams Channel Intelligence, for reuse — not copy-paste — by every agent built after it.

## What's in here

- `spine.llm` — the LLM gateway (provider swap, on-disk cache, retry/backoff, degrade-to-fallback) and the structured-output validate-and-retry layer.
- `spine.storage` — the SQLite connection/migration engine. Agent-specific schemas live in each agent's own migration files, not here.
- `spine.approval` — the proposal record and the write guard every outbound action goes through (nothing sends without an approved proposal).
- `spine.grounding` — reference-or-drop and verbatim-quote verification, independent of any agent-specific message type.
- `spine.prompts` — the prompt registry.
- `spine.eval` — the golden-case eval harness framework (case/metric types, runner, results store). Agent-specific golden cases live in that agent's own package.
- `spine.adapters` — the Teams reader/publisher interfaces (P2 reuses these directly as its own chat source).
- `spine.scheduling` — a generic cron-style scheduler: give it a way to turn your config into a `ScheduleSpec` and a job function, it handles the rest.
- `spine.config` — the committed-YAML-plus-live-DB-override config store pattern, generalized over any Pydantic model.

## What's deliberately NOT in here

Anything that encodes a decision specific to one agent's own problem: P1's `ChannelConfig` schema and its own `channel_config`/`messages`/etc. migration files, its golden-case definitions, its concrete Teams-message-table grounding lookup. Spine is the machinery; the agent-specific meaning built on top of it stays in that agent's own package.

## Install

```
pip install -e .
```

or, from a uv workspace root that includes this package as a member:

```
uv sync
```

## Test

```
pytest
```
