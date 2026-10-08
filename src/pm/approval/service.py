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
from datetime import datetime, timezone
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
from pm.approval.proposals import (
    BRIEF_PROPOSAL_TYPE,
    DIRECT_MESSAGE_TYPES,
    EOD_PROPOSAL_TYPE,
    ESCALATION_PROPOSAL_TYPE,
    NUDGE_PROPOSAL_TYPE,
)
from pm.channel.batches import CHANNEL_RISK_PROPOSAL_TYPE, CHANNEL_TRACKER_PROPOSAL_TYPE
from pm.commitments import delivery
from pm.mirror.hook import mirrored
from pm.reporting.morning_brief import _AS_RECORDED
from pm.scheduling.config import P1_CHANNEL_CONFIG_DIR
from pm.storage.db import DEFAULT_DB_PATH

# The only proposal types this service can carry out. A risk-log entry (PM-16) is
# reviewed here -- shown, rejected on the record -- but approving one would have
# nothing to execute, so it is refused up front rather than left half-approved.
EXECUTABLE_TYPES = frozenset({BRIEF_PROPOSAL_TYPE, EOD_PROPOSAL_TYPE}) | DIRECT_MESSAGE_TYPES
CHANNEL_BATCH_TYPES = frozenset({CHANNEL_TRACKER_PROPOSAL_TYPE, CHANNEL_RISK_PROPOSAL_TYPE})  # PM-26: proposals only; approving writes nothing

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


def _not_executable(proposal: Proposal) -> str | None:
    if proposal.type in EXECUTABLE_TYPES:
        return None
    return (
        f"a {proposal.type} proposal cannot be approved or sent: applying an approved risk entry "
        "is not built, so approving it would do nothing. It can be reviewed and rejected"
    )


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _batch_line(item: dict) -> str:
    """One item of a batch from P1's outcome record, as a person reads it: what it would do, and the message that justifies it."""
    ref = item["reference"]
    if item["kind"] == "comment":
        what = f"Comment on {item['item_id']}: {item['source_text']}"
    elif item["kind"] == "create":
        what = f"New item (blocked, nobody assigned): {item['title']}"
    else:
        owner = f" (suggested owner {item['suggested_owner']})" if item.get("suggested_owner") else ""
        what = f"New risk entry for {item.get('related_item_id') or 'no item'}{owner}: {item['title']}"
    line = f"- {what}\n    from {ref['channel_display_name']} message {ref['message_id']} on {ref['date']}"
    if item.get("changed_since"):
        line += f"\n    replaces an earlier wording ({item['changed_since']['earlier_status']}): {item['changed_since']['earlier_text']}"
    return line


def _summarize_batch(proposal: Proposal) -> PendingApproval:
    payload = proposal.payload
    items = payload.get("items", [])
    kind = "Tracker changes" if proposal.type == CHANNEL_TRACKER_PROPOSAL_TYPE else "Risk-log entries"
    return PendingApproval(
        proposal_id=proposal.id, type=proposal.type, target_channel=payload.get("channel_id", "?"), local_date=payload.get("date", "?"),
        created_at=proposal.created_at,
        summary=f"{kind} from {payload.get('channel_display_name', '?')} ({payload.get('date', '?')}): {len(items)} item{'' if len(items) == 1 else 's'}",
        content="\n".join(_batch_line(item) for item in items),
    )


def _summarize(proposal: Proposal) -> PendingApproval:
    payload = proposal.payload
    target = payload.get("target_channel", "?")
    date = payload.get("local_date", "?")
    summary = f"Morning brief for {date} to {target}"
    if proposal.type == EOD_PROPOSAL_TYPE:
        summary = f"End-of-day summary for {date} to {target}"
    if proposal.type == NUDGE_PROPOSAL_TYPE:
        summary = f"Reminder to {payload.get('recipient_name') or target} about commitment #{payload.get('commitment_id')}"
    if proposal.type == ESCALATION_PROPOSAL_TYPE:
        summary = f"Escalation to {payload.get('recipient_name') or target} about commitment #{payload.get('commitment_id')}"
    if proposal.type in CHANNEL_BATCH_TYPES:
        return _summarize_batch(proposal)
    if proposal.type not in EXECUTABLE_TYPES:
        summary = f"Proposed risk log entry for {payload.get('item_id', '?')} ({payload.get('blocker_ref', '?')})"
    return PendingApproval(
        proposal_id=proposal.id, type=proposal.type, target_channel=target, local_date=date,
        created_at=proposal.created_at, summary=summary, content=payload.get("content", ""),
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
    if proposal.type in DIRECT_MESSAGE_TYPES:  # a message to one person, not a channel post: no channel allowlist, but a named recipient
        return None if proposal.payload.get("recipient_id") else "this message has no recipient"
    if isinstance(publisher, LogPublisher):
        return None
    target = proposal.payload.get("target_channel")
    if target not in policy.allowlisted_channel_ids:
        return f"{target} is not on the channel allowlist, so a real post to it is not allowed"
    return None


@mirrored
def approve_and_send(
    proposal_id: str,
    *,
    approver_id: str,
    edited_content: str | None = None,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
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

    unsupported = _not_executable(proposal)
    if unsupported:
        return _deny(db_path, approver_id, proposal_id, "approve", unsupported)
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
    return _execute(proposal_id, actor=approver, publisher=publisher, policy=policy, store=store, db_path=db_path, now=now)


def approve_automatically(
    proposal_id: str, *, reason: str, store: ProposalStore | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """The system approves a pending reminder or escalation under the commitment follow-up's own rules (the person
    has been reminded before, within the cap). Recorded as automatic, under the system's own approver id, with the reason:
    never as a person. Raises IllegalTransitionError if it is not pending. Sending is a separate step (send_approved)."""
    store = store or ProposalStore(db_path)
    store.approve(proposal_id, approver_id=AUTO_APPROVER)
    write_audit(db_path, actor=AUTO_APPROVER, action="proposal.approved", proposal_id=proposal_id,
                details={"edited": False, "automatic": True, "reason": reason})


@mirrored
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


@mirrored
def send_approved(
    proposal_id: str,
    *,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> ActionResult:
    """Execute (or retry) an already-approved proposal. Refused for anything
    that is not approved -- pending, rejected, already sent, unknown. `now` is the
    clock a direct message's rules are judged by (a demo or a test can set it)."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - reported, never raised past this seam
        return ActionResult(proposal_id, REFUSED, f"publisher not available: {type(exc).__name__}: {exc}")
    return _execute(proposal_id, actor=AGENT, publisher=publisher, policy=policy, store=store, db_path=db_path, now=now)


def _execute_direct_message(proposal: Proposal, *, actor: str, publisher, store: ProposalStore, db_path, now: datetime) -> ActionResult:
    """A reminder to the person, or an escalation to the lead: a direct message, sent only after the rules in
    pm.commitments.delivery (the shared cap, the commitment still open) and recorded there afterwards."""
    problem = delivery.pre_send_problem(proposal, db_path, now)
    if problem:
        write_audit(db_path, actor=actor, action="proposal.send_refused", proposal_id=proposal.id, details={"reason": problem})
        return ActionResult(proposal.id, REFUSED, problem)
    recipient, content = proposal.payload["recipient_id"], proposal.payload.get("content", "")
    action = "nudge" if proposal.type == NUDGE_PROPOSAL_TYPE else "escalation"
    try:
        guarded_send(
            proposal.id, action_type=action, target=recipient,
            send_fn=lambda: publisher.post_direct_message(recipient, content), store=store, db_path=db_path,
        )
    except WriteRefusedError as exc:
        return ActionResult(proposal.id, REFUSED, str(exc))
    except Exception as exc:  # noqa: BLE001 - guarded_send already logged send_failed; report, never raise
        write_audit(db_path, actor=actor, action="proposal.send_failed", proposal_id=proposal.id,
                    details={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return ActionResult(proposal.id, SEND_FAILED, f"{type(exc).__name__}: {exc}")
    write_audit(db_path, actor=actor, action="proposal.sent", proposal_id=proposal.id, details={"target": recipient})
    delivery.after_send(proposal, db_path, now)
    return ActionResult(proposal.id, SENT, f"sent to {recipient}")


def _execute(
    proposal_id: str, *, actor: str, publisher, policy: ApprovalPolicy, store: ProposalStore, db_path, now: datetime | None = None,
) -> ActionResult:
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")
    unsupported = _not_executable(proposal)  # defence in depth: whatever its status, only a brief is posted
    if unsupported:
        return ActionResult(proposal_id, REFUSED, unsupported)
    scope_problem = _out_of_scope(proposal, publisher, policy)
    if scope_problem and proposal.status == APPROVED:
        return ActionResult(proposal_id, REFUSED, scope_problem)
    if proposal.type in DIRECT_MESSAGE_TYPES:
        return _execute_direct_message(proposal, actor=actor, publisher=publisher, store=store, db_path=db_path,
                                       now=now or datetime.now(timezone.utc))

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
        p.type in EXECUTABLE_TYPES
        and p.payload.get("target_channel") == target
        and p.approver_id not in (None, AUTO_APPROVER)
        for p in store.list_by_status(APPLIED)
    )


def _auto_hold_reason(proposal: Proposal, policy: ApprovalPolicy, publisher, store: ProposalStore) -> str | None:
    """Why auto-approve would NOT approve this pending proposal, or None if it
    would. The one place these rules live: auto_approve_and_send acts on it and
    explain_hold shows it, so they cannot disagree."""
    dropped = _dropped_count(proposal)
    if dropped or _AS_RECORDED in proposal.payload.get("content", ""):
        return f"grounding dropped {dropped} line(s) in this message; a person must review it"
    if policy.auto_approve_requires_first_human and not _a_person_has_approved_one_before(store, proposal):
        return "waiting for a person to approve a first brief for this channel"
    return _out_of_scope(proposal, publisher, policy)


def explain_hold(
    proposal_id: str,
    *,
    policy: ApprovalPolicy | None = None,
    publisher=None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str | None:
    """Why this brief is waiting for a person, in words; None means nothing is
    holding it back (auto-approve would take it on its next run)."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return f"no proposal with id {proposal_id!r}"
    if proposal.status != PENDING:
        return f"already {proposal.status}: nothing is waiting"
    if proposal.type in DIRECT_MESSAGE_TYPES:
        return "a reminder or an escalation: it is sent under the commitment follow-up's rules, or when a person approves it"
    if proposal.type not in EXECUTABLE_TYPES:
        return "this is a risk log entry proposal: auto-approve never takes it, so a person has to decide (it can only be rejected for now)"
    if not policy.auto_approve:
        return "auto-approve is off, so a person has to decide"
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - reported as the reason, not raised
        return f"publisher not available: {type(exc).__name__}: {exc}"
    return _auto_hold_reason(proposal, policy, publisher, store)


@mirrored
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
    if proposal.type in DIRECT_MESSAGE_TYPES:
        return _held(proposal_id, "a reminder or an escalation is approved by the commitment follow-up's own rules or by a person, never by brief auto-approve")
    if proposal.type not in EXECUTABLE_TYPES:
        return _held(proposal_id, "a risk log entry proposal is never auto-approved; a person has to decide")

    held = _auto_hold_reason(proposal, policy, publisher, store)
    if held:
        return _held(proposal_id, held)

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
