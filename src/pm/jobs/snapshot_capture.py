"""The step both scheduled jobs start with: take a snapshot of the project as
of one moment, in the project's own timezone, and persist it. Idempotent for a
moment: a repeat reads back what the first call stored.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pm.scheduling.config import ProjectScheduleConfig
from pm.state.snapshot import ProjectSnapshot, build_current_snapshot
from pm.state.store import DuplicateSnapshotError, read_snapshot, save_snapshot


def capture_snapshot(config: ProjectScheduleConfig, moment: datetime, *, db_path: str | Path) -> ProjectSnapshot:
    taken_at = moment.isoformat()
    snapshot = build_current_snapshot(db_path=db_path, taken_at=taken_at, tz_name=config.timezone)
    try:
        save_snapshot(snapshot, db_path=db_path)
    except DuplicateSnapshotError:
        # This exact moment was already run once -- read back what that
        # run persisted rather than discarding it or crashing a harmless repeat.
        snapshot = read_snapshot(taken_at, db_path=db_path)
    return snapshot
