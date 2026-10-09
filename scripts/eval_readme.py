#!/usr/bin/env python3
"""Put the headline eval numbers in README.md, rendered from eval/results.jsonl (PM-33). Nothing is typed by hand.

  --write   rewrite the block between the eval markers in README.md (or add it before "Getting started")
  --check   exit 1 if README.md no longer matches the results file (a number moved, or a run was recorded and the README was not updated)

Usage:
    uv run python scripts/eval_readme.py --write
    uv run python scripts/eval_readme.py --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.eval.headline import load_records, readme_matches, update_readme

README = _REPO_ROOT / "README.md"
RESULTS = _REPO_ROOT / "eval" / "results.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    records = load_records(RESULTS)
    readme = README.read_text(encoding="utf-8")
    if args.check:
        ok = readme_matches(readme, records)
        print("README.md matches eval/results.jsonl" if ok else "README.md does not match eval/results.jsonl: run scripts/eval_readme.py --write")
        return 0 if ok else 1
    README.write_text(update_readme(readme, records), encoding="utf-8")
    print("README.md updated from eval/results.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
