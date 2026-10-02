"""
Eval harness runner (SPN-07): runs every case in a GoldenCaseRegistry,
prints one line per metric (measured vs target, pass/fail -- this
task's own acceptance test), and appends one run record to the
committed results file -- tagged with whatever model_id and
prompt_versions the caller supplies, since the harness itself has no
opinion on which model or prompt produced the numbers it is checking.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from spine.eval.cases import GoldenCaseRegistry, MetricResult
from spine.eval.results_store import DEFAULT_RESULTS_PATH, append_run


@dataclass(frozen=True)
class EvalRunSummary:
    results: list[MetricResult]
    all_passed: bool


def run_eval(
    registry: GoldenCaseRegistry,
    *,
    model_id: str | None = None,
    prompt_versions: dict[str, str] | None = None,
    results_path: str | Path = DEFAULT_RESULTS_PATH,
    out: TextIO = sys.stdout,
) -> EvalRunSummary:
    all_results: list[MetricResult] = []

    for case in registry.all_cases():
        case_results = case.measure_fn()
        all_results.extend(case_results)
        for result in case_results:
            print(result.format_line(), file=out)

    all_passed = all(r.passed for r in all_results)

    record = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "model_id": model_id,
        "prompt_versions": prompt_versions or {},
        "results": [
            {
                "metric_id": r.metric_id,
                "name": r.name,
                "measured": r.measured,
                "target": r.target,
                "comparator": r.comparator_name,
                "passed": r.passed,
                "detail": r.detail,
            }
            for r in all_results
        ],
        "all_passed": all_passed,
    }
    append_run(record, results_path)

    return EvalRunSummary(results=all_results, all_passed=all_passed)