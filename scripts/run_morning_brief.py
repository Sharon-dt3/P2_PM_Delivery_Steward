#!/usr/bin/env python3
"""Build a real current-state snapshot and print a real morning brief,
generated through the real Claude API (spine.llm.gateway.LLMGateway) --
not a fake/test gateway. This is PM-08's own "Primary tool: Claude API
(expression) + Python (facts)" demonstrated for real, not just under
pytest with a FakeGateway.

Requires ANTHROPIC_API_KEY to be set (the same variable P1's own LLM
calls already need -- see spine/llm/gateway.py's own LLMGateway.__init__).
Makes one real, billed API call.

Usage:
    uv run python scripts/run_morning_brief.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same fix scripts/seed.py and scripts/run_eval.py already apply, for the
# same reason (see either script's own comment): uv's editable-install
# .pth redirect for path dependencies (spine, p1) is unreliable outside a
# handful of execution contexts, and pytest's `pythonpath` ini option only
# takes effect inside pytest's own import machinery.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _P1_REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.reporting.facts import compute_morning_brief_facts  # noqa: E402
from pm.reporting.morning_brief import generate_morning_brief  # noqa: E402
from pm.state.snapshot import build_current_snapshot  # noqa: E402
from spine.llm.gateway import LLMGateway  # noqa: E402


def main() -> None:
    snapshot = build_current_snapshot()
    facts = compute_morning_brief_facts(snapshot)

    gateway = LLMGateway()
    brief = generate_morning_brief(facts, gateway)

    print(f"Morning brief -- as of {facts.as_of}")
    print()
    print(brief.narrative)


if __name__ == "__main__":
    main()
