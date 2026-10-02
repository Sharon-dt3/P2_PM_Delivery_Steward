#!/usr/bin/env python3
"""P2's eval entry point.

Prints PM-07's golden case 3 report (precision/recall of the diff engine
against hand labels), then runs every case registered with spine's eval
harness (PM-12: GC1 citation rate, GC2 fabricated-claim count), printing
one line per metric and appending the run to eval/results.jsonl, tagged
with a model id and every prompt capability's current version.

The default run drives the brief through a scripted gateway, so its
model id is "scripted-gateway": the numbers measure P2's grounding
machinery, not a language model, and the results file says so rather than
naming a model that was never called. Pass --model-id to tag a run made
with a different gateway deliberately.

Usage:
    uv run python scripts/run_eval.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Same fix scripts/seed.py already applies, for the same reason (see that
# script's own comment, and P3_Agents/DECISION_LOG.md's CHN-33 entry):
# uv's editable-install .pth redirect for path dependencies (spine, p1) is
# unreliable outside a handful of execution contexts, and pytest's
# `pythonpath` ini option only takes effect inside pytest's own import
# machinery -- a bare script run via `uv run python scripts/run_eval.py`
# needs the exact same three paths inserted here directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _P1_REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval
from spine.prompts.registry import PromptRegistry

from pm.eval.registrations import register_all
from pm.eval.runner import main as print_golden_case_3

RESULTS_PATH = _REPO_ROOT / "eval" / "results.jsonl"
DEFAULT_MODEL_ID = "scripted-gateway"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    args = parser.parse_args()

    print_golden_case_3()
    print()

    registry = GoldenCaseRegistry()
    register_all(registry)
    prompts = PromptRegistry(_REPO_ROOT / "prompts")
    summary = run_eval(
        registry,
        model_id=args.model_id,
        prompt_versions={name: prompts.get(name).version for name in prompts.list_capabilities()},
        results_path=RESULTS_PATH,
    )
    return 0 if summary.all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
