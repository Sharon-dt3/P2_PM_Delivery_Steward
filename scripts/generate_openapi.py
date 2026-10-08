#!/usr/bin/env python3
"""Write the OpenAPI document a Power Platform custom connector imports (docs/copilot_studio/openapi.json).

It is generated from the running API, never edited by hand; tests/unit/test_copilot_api.py fails if the file drifts from the API.

With --connector URL it writes the file to IMPORT in Power Automate instead (data/pm_connector_openapi.json): OpenAPI 3.0 with
URL as its server, for whatever address the API is reachable at.

Usage:
    uv run python scripts/generate_openapi.py
    uv run python scripts/generate_openapi.py --connector https://your-address [--skip-ngrok-warning]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.api.connector_spec import connector_spec
from pm.api.copilot_studio_api import app

TARGET = _REPO_ROOT / "docs" / "copilot_studio" / "openapi.json"

CONNECTOR_TARGET = _REPO_ROOT / "data" / "pm_connector_openapi.json"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connector", metavar="URL", help="write the file to import as a custom connector, for this server address")
    parser.add_argument("--skip-ngrok-warning", action="store_true",
                        help="make every action send ngrok-skip-browser-warning (needed behind a free ngrok tunnel)")
    args = parser.parse_args()
    if args.connector:
        CONNECTOR_TARGET.write_text(json.dumps(connector_spec(
            app.openapi(), args.connector, always_send={"ngrok-skip-browser-warning": "1"} if args.skip_ngrok_warning else None), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {CONNECTOR_TARGET.relative_to(_REPO_ROOT)} for {args.connector}")
    else:
        TARGET.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {TARGET.relative_to(_REPO_ROOT)}")
