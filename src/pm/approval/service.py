"""PM-13: the approval service -- the one place the gate is enforced.

Cards, the command line and any future surface call exactly these functions
and nothing else, so they cannot get different answers or leave different
records. Rules enforced here, never in a surface:

- only someone on the approver list may approve or reject (fail closed: with
  no approvers configured, nobody can); a refused attempt is itself recorded;
- who is acting is a parameter the caller's platform supplies; nothing a card
  submits can change it;
- a proposal is executed only after a positive approval, through spine's
  guarded_send (which refuses anything not approved, whoever calls it), and
  every attempt is logged (write_log) and audited;
- an edit replaces what is sent but never what the agent originally proposed;
- a real (non-log) publisher may only post to a channel on P1's allowlist, and
  that is checked BEFORE approving, so an out-of-scope proposal cannot be
  approved into a stuck state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import (
    APPLIED,
    APPROVED,
    PENDING,
    IllegalTransitionError,
    Proposal,
    ProposalNotFoundError,
    ProposalStore,
)
from spine.approval.write_guard import WriteRefusedError, guarded_send

from pm.adapters.teams import get_teams_publisher
from pm.approval.audit import AGENT, AUTO_APPROVER, write_audit
from pm.approval.proposals import BRIEF_PROPOSAL_TYPE
from pm.reporting.morning_brief import _AS_RECORDED
from pm.scheduling.config import P1_CHANNEL_CONFIG_DIR
from pm.storage.db import DEFAULT_DB_PATH

SENT = "sent"
REJECTED_OUTCOME = "rejected"
REFUSED = "refused"
SEND_FAILED = "send_failed"
HELD = "held"  # auto-approve declined: a person has to decide



@dataclass(frozen=True)
class ApprovalPolicy:
    approver_ids: frozenset[str] = frozenset()
    allowlisted_channel_ids: list[str] = field(default_factory=list)
    auto_approve: bool = False  # PM_AUTO_APPROVE=1: the system approves safe briefs itself
    auto_approve_requires_first_human: bool = True  # a person must have approved one for the channel first


@dataclass(frozen=True)
class ActionResult:
    proposal_id: str
    outcome: str  # sent | rejected | refused | send_failed
    detail: str


@dataclass(frozen=True)
class PendingApproval:
    proposal_id: str
    type: str
    target_channel: str
    local_date: str
    created_at: str
    summary: str
    content: str


def load_approval_policy(config_dir: str | Path = P1_CHANNEL_CONFIG_DIR) -> ApprovalPolicy:
    """Approvers are the comma-separated ids in PM_APPROVER_IDS (none by
    default); the channel allowlist is P1's own (config/channels). Auto-approve
    is on only when PM_AUTO_APPROVE is exactly "1"; its first-approval rule is
    on unless PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN is exactly "0"."""
    from p1.config.loader import ChannelConfigStore

    raw = os.environ.get("PM_APPROVER_IDS", "")
    return ApprovalPolicy(
        approver_ids=frozenset(part.strip() for part in raw.split(",") if part.strip()),
        allowlisted_channel_ids=ChannelConfigStore(config_dir).list_allowlisted_channels(),
        auto_approve=os.environ.get("PM_AUTO_APPROVE", "") == "1",
        auto_approve_requires_first_human=os.environ.get("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", "1") != "0",
    )


def _is_approver(approver_id: str, policy: ApprovalPolicy) -> bool:
    who = (approver_id or "").strip()
    return bool(who) and who != AUTO_APPROVER and who in policy.approver_ids


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _summarize(proposal: Proposal) -> PendingApproval:
    payload = proposal.payload
    target = payload.get("target_channel", "?")
    date = payload.get("local_date", "?")
    return PendingApproval(
        proposal_id=proposal.id, type=proposal.type, target_channel=target, local_date=date,
        created_at=proposal.created_at, summary=f"Morning brief for {date} to {target}",
        content=payload.get("content", ""),
    )


def list_pending_approvals(
    *, store: ProposalStore | None = None, db_path: str | Path = DEFAULT_DB_PATH
) -> list[PendingApproval]:
    store = store or ProposalStore(db_path)
    return [_summarize(p) for p in store.list_by_status(PENDING)]


def _deny(db_path, approver_id: str, proposal_id: str, attempted: str, why: str) -> ActionResult:
    write_audit(
        db_path, actor=(approver_id or "").strip() or "(blank)", action="proposal.denied",
        proposal_id=proposal_id, details={"attempted": attempted, "reason": why},
    )
    return ActionResult(proposal_id, REFUSED, why)


def _out_of_scope(proposal: Proposal, publisher, policy: ApprovalPolicy) -> str | None:
    """A real publisher may only post to an allowlisted channel; the log-only
    one posts nowhere, so it needs no allowlist."""
    if isinstance(publisher, LogPublisher):
        return None
    target = proposal.payload.get("target_channel")
    if target not in policy.allowlisted_channel_ids:
        return f"{target} is not on the channel allowlist, so a real post to it is not allowed"
    return None


def approve_and_send(
    proposal_id: str,
    *,
    approver_id: str,
    edited_content: str | None = None,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> ActionResult:
    """Approve a pending proposal -- optionally with edited text -- and execute
    it through the adapter in the same call."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)

    if not _is_approver(approver_id, policy):
        return _deny(db_path, approver_id, proposal_id, "approve", "not an authorised approver")
    approver = approver_id.strip()
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - a misconfigured publisher means nothing is approved, nothing raised
        return ActionResult(proposal_id, REFUSED, f"publisher not available: {type(exc).__name__}: {exc}")
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")

    scope_problem = _out_of_scope(proposal, publisher, policy)
    if scope_problem:
        return ActionResult(proposal_id, REFUSED, scope_problem)

    final_payload = None
    edited = False
    if edited_content is not None:
        text = _normalise(edited_content)
        if not text:
            return ActionResult(proposal_id, REFUSED, "the edited text is empty")
        if text != _normalise(proposal.payload.get("content", "")):
            edited = True
            final_payload = {**proposal.payload, "content": text}

    try:
        store.approve(proposal_id, approver_id=approver, payload=final_payload)
    except IllegalTransitionError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))

    if edited:
        write_audit(
            db_path, actor=approver, action="proposal.edited", proposal_id=proposal_id,
            details={
                "original_length": len(proposal.payload.get("content", "")),
                "final_length": len(final_payload["content"]),
            },
        )
    write_audit(db_path, actor=approver, action="proposal.approved", proposal_id=proposal_id, details={"edited": edited})
    return _execute(proposal_id, actor=approver, publisher=publisher, policy=policy, store=store, db_path=db_path)


def reject(
    proposal_id: str,
    *,
    approver_id: str,
    reason: str | None = None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> ActionResult:
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    if not _is_approver(approver_id, policy):
        return _deny(db_path, approver_id, proposal_id, "reject", "not an authorised approver")
    approver = approver_id.strip()
    try:
        store.reject(proposal_id, approver_id=approver)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")
    except IllegalTransitionError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))
    write_audit(db_path, actor=approver, action="proposal.rejected", proposal_id=proposal_id, details={"reason": reason})
    return ActionResult(proposal_id, REJECTED_OUTCOME, "rejected")


def send_approved(
    proposal_id: str,
    *,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> ActionResult:
    """Execute (or retry) an already-approved proposal. Refused for anything
    that is not approved -- pending, rejected, already sent, unknown."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - reported, never raised past this seam
        return ActionResult(proposal_id, REFUSED, f"publisher not available: {type(exc).__name__}: {exc}")
    return _execute(proposal_id, actor=AGENT, publisher=publisher, policy=policy, store=store, db_path=db_path)


def _execute(proposal_id: str, *, actor: str, publisher, policy: ApprovalPolicy, store: ProposalStore, db_path) -> ActionResult:
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")
    scope_problem = _out_of_scope(proposal, publisher, policy)
    if scope_problem and proposal.status == APPROVED:
        return ActionResult(proposal_id, REFUSED, scope_problem)

    target = proposal.payload.get("target_channel", "")
    content = proposal.payload.get("content", "")
    try:
        guarded_send(
            proposal_id, action_type="channel_post", target=target,
            send_fn=lambda: publisher.post_channel_message(target, content), store=store, db_path=db_path,
        )
    except WriteRefusedError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))
    except Exception as exc:  # noqa: BLE001 - guarded_send already logged send_failed; report, never raise
        write_audit(
            db_path, actor=actor, action="proposal.send_failed", proposal_id=proposal_id,
            details={"error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        return ActionResult(proposal_id, SEND_FAILED, f"{type(exc).__name__}: {exc}")

    write_audit(db_path, actor=actor, action="proposal.sent", proposal_id=proposal_id, details={"target": target})
    return ActionResult(proposal_id, SENT, f"sent to {target}")


def _held(proposal_id: str, why: str) -> ActionResult:
    return ActionResult(proposal_id, HELD, why)


def _dropped_count(proposal: Proposal) -> int:
    dropped = proposal.original_model_output.get("dropped") or {}
    return sum(len(failures) for failures in dropped.values())


def _a_person_has_approved_one_before(store: ProposalStore, proposal: Proposal) -> bool:
    target = proposal.payload.get("target_channel")
    return any(
        p.type == BRIEF_PROPOSAL_TYPE
        and p.payload.get("target_channel") == target
        and p.approver_id not in (None, AUTO_APPROVER)
        for p in store.list_by_status(APPLIED)
    )


def auto_approve_and_send(
    proposal_id: str,
    *,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> ActionResult:
    """Unattended approval: the system approves a pending proposal itself and
    executes it -- only when that is safe, otherwise HELD for a person.

    Safe means: auto-approve is switched on; grounding dropped nothing and no
    fact fell back to "[as recorded]"; (unless switched off) a person has
    already approved a brief for this channel; and a real publisher's target is
    on the allowlist. The approval is recorded under AUTO_APPROVER, as
    automatic, with the reason -- never as a person -- and the text is never
    edited. Everything after that is the ordinary gate (guarded_send, logs)."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    if not policy.auto_approve:
        return _held(proposal_id, "auto-approve is off")
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - a misconfigured publisher means it stays proposed
        return _held(proposal_id, f"publisher not available: {type(exc).__name__}: {exc}")
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")
    if proposal.status != PENDING:
        return ActionResult(proposal_id, REFUSED, f"proposal is {proposal.status!r}, not pending")

    dropped = _dropped_count(proposal)
    if dropped or _AS_RECORDED in proposal.payload.get("content", ""):
        return _held(proposal_id, f"grounding dropped {dropped} line(s) in this brief; a person must review it")
    if policy.auto_approve_requires_first_human and not _a_person_has_approved_one_before(store, proposal):
        return _held(proposal_id, "waiting for a person to approve a first brief for this channel")
    scope_problem = _out_of_scope(proposal, publisher, policy)
    if scope_problem:
        return _held(proposal_id, scope_problem)

    reason = "every line grounded; " + (
        "a person had already approved a brief for this channel"
        if policy.auto_approve_requires_first_human else "the first-approval rule is switched off"
    )
    try:
        store.approve(proposal_id, approver_id=AUTO_APPROVER)
    except IllegalTransitionError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))
    write_audit(
        db_path, actor=AUTO_APPROVER, action="proposal.approved", proposal_id=proposal_id,
        details={"edited": False, "automatic": True, "reason": reason},
    )
    return _execute(proposal_id, actor=AUTO_APPROVER, publisher=publisher, policy=policy, store=store, db_path=db_path)
