"""What is switched on, in words, for anything that should tell a person before
they act (the runner's start-up banner, the approval dashboard). Never includes a
secret: the Power Automate flow URL is a credential and is not printed.
"""

from __future__ import annotations

import os

from p1.adapters.teams_publisher_mock import LogPublisher

from pm.adapters.teams import get_teams_publisher


def describe_settings() -> list[str]:
    try:
        publisher = get_teams_publisher()
        where = (
            "log-only (nothing reaches Teams)" if isinstance(publisher, LogPublisher)
            else "REAL: posts through the Power Automate flow"
        )
    except Exception as exc:  # noqa: BLE001 - misconfiguration is reported, not raised
        where = f"NOT AVAILABLE ({type(exc).__name__}: {exc})"
    auto = os.environ.get("PM_AUTO_APPROVE", "") == "1"
    first = os.environ.get("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", "1") != "0"
    approvers = [a.strip() for a in os.environ.get("PM_APPROVER_IDS", "").split(",") if a.strip()]
    return [
        f"publisher: {where}",
        f"auto-approve: {'ON' if auto else 'off'}"
        + (f" (a person must approve the first brief per channel: {'yes' if first else 'no'})" if auto else ""),
        f"approvers: {', '.join(approvers) if approvers else 'none set (PM_APPROVER_IDS)'}",
    ]
