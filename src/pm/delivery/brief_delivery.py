"""PM-11, Power Automate half: hand a scheduled brief to a Teams publisher.

Which publisher is configuration (pm.adapters.teams.get_teams_publisher, the
same TEAMS_PUBLISHER_MODE switch P1's factory uses). What this module adds is
the safety around using it:

- log-only (P1's LogPublisher) is always allowed: nothing leaves the machine;
- anything else is a real post and needs BOTH PM_ALLOW_LIVE_POST=1 and a target
  channel on P1's allowlist. Anything unrecognised counts as real (fail closed);
- one brief per kind, channel and local day (the `deliveries` table), so a
  restart or catch-up cannot post it twice; a deliberate redelivery is possible;
- a send that fails is reported, not raised, and is not recorded as delivered,
  so a retry can succeed.

This is NOT the approval gate. PM-13 wires scheduled output onto the proposal
spine (guarded_send) and replaces the PM_ALLOW_LIVE_POST switch; until then
this is what keeps a real post from happening by accident.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from p1.adapters.teams_publisher import TeamsPublisher
from p1.adapters.teams_publisher_mock import LogPublisher

from pm.scheduling.config import P1_CHANNEL_CONFIG_DIR
from pm.storage.db import DEFAULT_DB_PATH, get_connection

logger = logging.getLogger(__name__)

DELIVERED = "delivered"
ALREADY_DELIVERED = "already_delivered"
REFUSED_LIVE_POSTING_DISABLED = "refused_live_posting_disabled"
REFUSED_CHANNEL_NOT_ALLOWLISTED = "refused_channel_not_allowlisted"
FAILED = "failed"
NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class DeliveryPolicy:
    live_posting_enabled: bool = False
    allowlisted_channel_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    detail: str = ""
    response: dict | None = None


def format_brief_message(label: str, local_date: str, content: str) -> str:
    """The exact text posted for a morning brief: an optional label, a dated
    title, then the brief itself and nothing else."""
    return f"{label}Morning brief — {local_date}\n\n{content}"


def load_delivery_policy(config_dir: str | Path = P1_CHANNEL_CONFIG_DIR) -> DeliveryPolicy:
    """Live posting is on only when PM_ALLOW_LIVE_POST is exactly "1"; the
    allowlist is P1's own (config/channels, allowlisted: true)."""
    from p1.config.loader import ChannelConfigStore

    return DeliveryPolicy(
        live_posting_enabled=os.environ.get("PM_ALLOW_LIVE_POST", "") == "1",
        allowlisted_channel_ids=ChannelConfigStore(config_dir).list_allowlisted_channels(),
    )


def _already_delivered(kind: str, channel_id: str, local_date: str, db_path: str | Path) -> bool:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM deliveries WHERE kind = ? AND channel_id = ? AND local_date = ?",
            (kind, channel_id, local_date),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _record_delivery(kind: str, channel_id: str, local_date: str, db_path: str | Path) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO deliveries (kind, channel_id, local_date, delivered_at) VALUES (?, ?, ?, ?)",
            (kind, channel_id, local_date, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def deliver_brief(
    content: str,
    *,
    kind: str,
    target_channel_id: str,
    local_date: str,
    publisher: TeamsPublisher,
    policy: DeliveryPolicy,
    db_path: str | Path = DEFAULT_DB_PATH,
    redeliver: bool = False,
) -> DeliveryResult:
    if not redeliver and _already_delivered(kind, target_channel_id, local_date, db_path):
        return DeliveryResult(ALREADY_DELIVERED, f"{kind} for {local_date} already delivered to {target_channel_id}")

    if not isinstance(publisher, LogPublisher):  # a real post: fail closed
        if not policy.live_posting_enabled:
            logger.warning("delivery_refused live posting is not enabled (PM_ALLOW_LIVE_POST) target=%s", target_channel_id)
            return DeliveryResult(REFUSED_LIVE_POSTING_DISABLED, "live posting is not enabled (PM_ALLOW_LIVE_POST=1)")
        if target_channel_id not in policy.allowlisted_channel_ids:
            logger.warning("delivery_refused channel is not allowlisted target=%s", target_channel_id)
            return DeliveryResult(REFUSED_CHANNEL_NOT_ALLOWLISTED, f"{target_channel_id} is not on the channel allowlist")

    try:
        response = publisher.post_channel_message(target_channel_id, content)
    except Exception as exc:  # noqa: BLE001 - a failed send must never take the scheduled job down
        logger.warning("delivery_failed target=%s error=%s: %s", target_channel_id, type(exc).__name__, exc)
        return DeliveryResult(FAILED, f"{type(exc).__name__}: {exc}")

    _record_delivery(kind, target_channel_id, local_date, db_path)
    return DeliveryResult(DELIVERED, f"{kind} delivered to {target_channel_id}", response)
