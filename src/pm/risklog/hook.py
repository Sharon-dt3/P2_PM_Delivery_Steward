"""Pull the lead's risk-log edits before the morning brief is built.

Only when PM_RISK_LOG_SYNC is exactly "1"; and only for the configured database, so a
leaked setting can never let a test or temporary database rewrite the committed CSV.
It never raises and never stops the brief: if the lead's store is unreachable, or the
two sides conflict, the brief is built from the repo copy (offline and reproducible)
and the outcome is logged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pm.mirror.supabase_mirror import _is_the_configured_database, _scrub
from pm.risklog.csv_store import CsvRiskLog
from pm.risklog.supabase_store import SupabaseRiskLog
from pm.risklog.sync import DEFAULT_BASELINE_PATH, RiskLogSync

logger = logging.getLogger("pm.risklog")


def _build_sync(db_path: str | Path) -> RiskLogSync:
    url = os.environ.get("SUPABASE_DB_URL", "")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL is not set")
    remote = SupabaseRiskLog(url, schema=os.environ.get("PM_SUPABASE_SCHEMA", "p2"))
    baseline = os.environ.get("PM_RISK_LOG_BASELINE") or DEFAULT_BASELINE_PATH
    return RiskLogSync(CsvRiskLog(), remote, db_path=db_path, baseline_path=baseline)


def pull_lead_edits_if_enabled(db_path: str | Path) -> str:
    """The outcome in one word: off, skipped, error, or the sync's own action."""
    if os.environ.get("PM_RISK_LOG_SYNC", "") != "1":
        return "off"
    if not _is_the_configured_database(db_path):
        return "skipped"
    try:
        outcome = _build_sync(db_path).sync()
    except Exception as exc:  # noqa: BLE001 - never stops the brief
        logger.warning("risk log sync failed: %s", _scrub(f"{type(exc).__name__}: {exc}", os.environ.get("SUPABASE_DB_URL", "")))
        return "error"
    if outcome.action not in ("in_sync", "pushed", "pulled"):
        logger.warning("risk log sync: %s: %s", outcome.action, outcome.detail)
    return outcome.action
