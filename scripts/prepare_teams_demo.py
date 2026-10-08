#!/usr/bin/env python3
"""Build a throwaway database for trying the Teams approval cards (PM-28): data/pm_demo.db.

A fresh copy of the frozen seed with proposals waiting for a decision, so approving in Teams never touches data/pm.db:
  - a morning brief (Approve / Reject)
  - the two batches made from P1's newest outcome record, if there is one (the risk batch: severity, Approve, Reject; the tracker
    batch: Approve, Reject). Approving the risk batch writes to the risk log and approving the tracker batch writes to the tracker, so
    try them against this throwaway database, with PM_RISK_LOG_CSV pointed at a COPY of risk_log/risks.csv
Rebuilding replaces the file. Nothing is sent anywhere.

Usage:
    uv run python scripts/prepare_teams_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
for _path in (_REPO_ROOT / "src", _P1_REPO_ROOT / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.tracker import TrackerMock
from pm.channel.batches import TrackerView, consume
from pm.eval.pm12_cases import ScriptedGateway
from pm.eval.pristine import build_pristine_database
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

TARGET = _REPO_ROOT / "data" / "pm_demo.db"


def _has_content(path: Path) -> bool:
    from pm.channel.record import RecordRefused, load_record

    try:
        return bool(load_record(path).evidence())
    except RecordRefused:
        return False


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(str(TARGET) + suffix).unlink(missing_ok=True)
    built = build_pristine_database(TARGET.parent)  # writes pristine.db next to the target
    built.rename(TARGET)
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    brief = run_morning_brief_job(config, ScriptedGateway(), moment=datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc), db_path=TARGET)
    print(f"morning brief proposal: {brief.proposal_id[:8]}")
    outcomes = Path(os.environ.get("P1_OUTCOMES_DIR") or _P1_REPO_ROOT / "outcomes")
    records = sorted(outcomes.glob("*/*.json"), key=lambda p: (p.stat().st_mtime, str(p)), reverse=True)
    chosen = next((p for p in records if _has_content(p)), None)  # the newest record that actually says something
    if chosen:
        view = TrackerView.from_adapters(TrackerMock(db_path=TARGET), RiskLogMock(db_path=TARGET))
        result = consume(chosen, view, db_path=TARGET)
        print(f"outcome record {chosen.parent.name[:24]}/{chosen.name}: " + (
            f"tracker batch {result.tracker.items} item(s), risk batch {result.risk.items} item(s)" if result.refused is None else f"refused ({result.refused.code})"))
    print(f"demo database: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
