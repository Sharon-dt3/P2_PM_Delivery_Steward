"""Where a refused outcome record is logged: one row in the audit log per consumer per attempt, with the code and the reason.

Both things that read a record (the PM-26 batches and the PM-24 commitment feed) refuse the same way and log the same way, so
"refused with a logged reason" means the same thing wherever the record was about to go.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pm.approval.audit import AGENT
from pm.channel.record import RecordRefused
from pm.storage.db import get_connection

BATCHES, COMMITMENT_FEED = "channel_batches", "commitment_feed"


def log_refusal(db_path: str | Path, path: str | Path, refusal: RecordRefused, *, consumer: str) -> None:
    path = Path(path)
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO audit (actor, action, entity_type, entity_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (AGENT, "record.refused", "outcome_record", path.name,
             json.dumps({"code": refusal.code, "reason": refusal.reason, "file": str(path), "consumer": consumer}),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
