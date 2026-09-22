#!/usr/bin/env python3
"""Print PM-07's golden case 3 report: precision and recall of PM-06's
diff engine against hand labels, over this repo's own seeded fixture.

Usage:
    uv run python scripts/run_eval.py
"""

from __future__ import annotations

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

from pm.eval.runner import main  # noqa: E402

if __name__ == "__main__":
    main()
