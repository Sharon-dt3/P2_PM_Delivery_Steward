#!/usr/bin/env python3
"""Write the OpenAPI document a Power Platform custom connector imports (docs/copilot_studio/openapi.json).

It is generated from the running API, never edited by hand; tests/unit/test_copilot_api.py fails if the file drifts from the API.

Usage:
    uv run python scripts/generate_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.api.copilot_studio_api import app

TARGET = _REPO_ROOT / "docs" / "copilot_studio" / "openapi.json"

if __name__ == "__main__":
    TARGET.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(_REPO_ROOT)}")
