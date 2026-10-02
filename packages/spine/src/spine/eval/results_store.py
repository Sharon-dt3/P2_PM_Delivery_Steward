"""
Results store (SPN-07): appends one JSON line per eval run to a
committed file -- eval/results.jsonl by default -- so every run's
numbers stay in the repo's own history, comparable across prompt
versions and model IDs over time. Append-only deliberately: a run never
edits or replaces a past run's line, matching this project's other
append-only audit artefacts (DECISION_LOG.md).
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_RESULTS_PATH = Path("eval/results.jsonl")


def append_run(record: dict, results_path: str | Path = DEFAULT_RESULTS_PATH) -> None:
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a") as f:
        f.write(json.dumps(record) + "\n")