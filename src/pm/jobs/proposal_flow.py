"""What both scheduled jobs do with a finished message (the morning brief, the end-of-day summary):
put it up for approval, and, when auto-approve is on and the message is safe to send unattended, have
the system approve it. One implementation, so the two cannot drift apart. A failure here is reported,
never raised: the message is already made and the scheduled job must not die on it."""

from __future__ import annotations

from collections.abc import Callable

from spine.approval.proposals import Proposal

from pm.adapters.teams import get_teams_publisher
from pm.approval.proposals import (
    ALREADY_PROPOSED,
    AUTO_SEND_FAILED,
    AUTO_SENT,
    FAILED,
    PROPOSED,
)
from pm.approval.service import (
    SEND_FAILED,
    SENT,
    auto_approve_and_send,
    load_approval_policy,
)


def propose_for_approval(
    propose: Callable[[], tuple[Proposal, bool]], *, local_date: str, db_path, publisher, policy
) -> tuple[str, str, str | None]:
    """`propose()` makes (or finds) the proposal and returns (proposal, created). Returns
    (delivery status, detail, proposal id)."""
    try:
        proposal, created = propose()
    except Exception as exc:  # noqa: BLE001 - a failed proposal must not take the scheduled job down
        return FAILED, f"{type(exc).__name__}: {exc}", None
    if not created:
        return ALREADY_PROPOSED, f"a proposal for {local_date} already exists (status {proposal.status})", proposal.id

    waiting = f"awaiting approval (proposal {proposal.id})"
    try:
        policy = policy if policy is not None else load_approval_policy()
    except Exception as exc:  # noqa: BLE001 - if the policy cannot be read, nothing is sent unattended
        return PROPOSED, f"{waiting}; auto-approve not applied: {type(exc).__name__}: {exc}", proposal.id
    if not policy.auto_approve:
        return PROPOSED, waiting, proposal.id

    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - a misconfigured publisher leaves it waiting for a person
        return PROPOSED, f"{waiting}; auto-approve not applied: {type(exc).__name__}: {exc}", proposal.id
    outcome = auto_approve_and_send(proposal.id, publisher=publisher, policy=policy, db_path=db_path)
    if outcome.outcome == SENT:
        return AUTO_SENT, f"auto-approved and sent (proposal {proposal.id})", proposal.id
    if outcome.outcome == SEND_FAILED:
        return AUTO_SEND_FAILED, f"auto-approved but the send failed: {outcome.detail}", proposal.id
    return PROPOSED, f"{waiting}; held for a person: {outcome.detail}", proposal.id
