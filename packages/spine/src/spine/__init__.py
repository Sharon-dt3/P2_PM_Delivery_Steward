"""
spine: the shared internal package extracted at Gate G1 (CHN-33).

Ten components built once during P1 and reused, not copy-pasted, by
every agent built after it (P2, P3, ...): the LLM gateway and its
structured-output/retry layer, the SQLite schema/migration engine, the
proposal record and write guard (the human-approval spine every
outbound action goes through), the grounding kernel (reference-or-drop
+ verbatim quote verification -- deliberately independent of Teams or
any P1-specific type), the prompt registry, the eval harness framework,
a generic cron-style scheduler, and a generic committed-config-plus-
live-override config store pattern.

What did NOT move here, on purpose: anything that encodes a *decision*
specific to P1's own problem (P1's ChannelConfig schema, its own
channel_config/messages/etc. migration files, its golden-case
definitions, its concrete Teams-message-table grounding lookup). Spine
is the machinery; the agent-specific meaning built on top of it stays
in that agent's own package. See each submodule's docstring for the
line it draws.
"""

from __future__ import annotations

__all__: list[str] = []
