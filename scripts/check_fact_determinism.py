#!/usr/bin/env python3
"""Golden case 9, by hand: generate the morning brief twice from one snapshot and compare the facts.

The wording of the two briefs may differ; the set of items, their owners, the counts and each item's
status may not. This prints the comparison, moment by moment, and exits 1 if anything diverges.

  --gateway scripted   two scripted stand-ins that word every line differently (instant, no model)
  --gateway llm        the model set by LLM_PROVIDER (Claude on Bedrock today), twice, each with its own
                       cache so the wording can really differ. Makes real, billed calls.

Usage:
    uv run python scripts/check_fact_determinism.py
    uv run python scripts/check_fact_determinism.py --gateway llm
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.eval.pm23_cases import (
    format_report,
    metrics,
    run_all,
    scripted_pair,
)


def _llm_pair():
    from spine.llm.gateway import LLMGateway

    work = Path(tempfile.mkdtemp(prefix="pm_gc9_live_"))
    return tuple(
        LLMGateway(cache_dir=work / f"cache_{n}", call_log_path=work / f"calls_{n}.jsonl") for n in (1, 2)
    )


def main(argv: list[str] | None = None, *, gateway_pair=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", choices=["scripted", "llm"], default="scripted")
    args = parser.parse_args(argv)
    pair = gateway_pair or (scripted_pair if args.gateway == "scripted" else _llm_pair)

    with tempfile.TemporaryDirectory(prefix="pm_gc9_") as tmp:
        runs = run_all(Path(tmp), pair)
    print(format_report(runs))
    results = metrics(runs)
    for result in results:
        print(result.format_line())
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
    raise SystemExit(main())
