#!/usr/bin/env python3
"""Rebuild this repo's own seeded fixture from scratch: the SQLite db
(sprints/assignees/items/transitions/comments/commits/commitments/
risks) plus the two P1 OutcomeRecord JSON fixtures. Safe to run any
number of times -- see build_seed()/build_outcome_fixtures()'s own
docstrings in src/pm/seed/build.py.

Usage:
    uv run python scripts/seed.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# uv's editable-install .pth redirect for path dependencies (spine, p1)
# is unreliable outside a handful of execution contexts -- confirmed for
# this exact repo pair on 2026-09-21 (see P3_Agents/DECISION_LOG.md's
# CHN-33 entry: it's what made pytest's own `pythonpath` ini option
# necessary there instead of relying on the .pth files that `uv sync`
# writes into site-packages). pytest's `pythonpath` option only takes
# effect *inside pytest's own import machinery*, so a bare script run
# via `uv run python scripts/seed.py` -- this one -- needs the exact
# same three paths inserted here directly, the same fix P1 itself
# applies to every one of its own non-pytest entry points.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _P1_REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.seed.build import build_outcome_fixtures, build_seed  # noqa: E402
from pm.storage.db import DEFAULT_DB_PATH, get_connection, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("pm.scripts.seed")


def main() -> None:
    init_db(DEFAULT_DB_PATH)
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        build_seed(conn)
    finally:
        conn.close()
    logger.info("Seeded %s", DEFAULT_DB_PATH)

    paths = build_outcome_fixtures()
    for path in paths:
        logger.info("Wrote %s", path)


if __name__ == "__main__":
    main()
